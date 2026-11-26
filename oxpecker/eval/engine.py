"""EvalEngine — orchestrates one retrospective evaluation pass.

For each asset: sample historical windows (deterministic), answer each with
the current knowledge base via the shared QA path, review against a
deterministic ground-truth pack, and append every case to the asset's
evals.jsonl ledger. Failures are honest: unknown verdicts stay out of the
accuracy denominator, a provider outage fails the pass without fabricating
scores, and everything is auditable in the runlog.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from ..config.model import AdviserConfig, AssetSpec, ModelRole
from ..core.asset_store import AssetStore
from ..core.runlog import log_run_event
from ..qa.engine import QAEngine
from .reviewer import EvalVerdict, ground_truth_pack, review
from .sampler import EvalCase, eval_date_key, sample_cases

_ANSWER_MAX_CHARS = 2000
_EVAL_LEDGER_CAP = 2000  # rows per asset; same retention discipline as runlog


class EvalEngine:
    def __init__(self, config: AdviserConfig, store: AssetStore,
                 gateway: Any, reviewer_role: str = "routine"):
        self.config = config
        self.store = store
        self.gateway = gateway
        self.qa = QAEngine(config, store, gateway)
        self.reviewer_role = reviewer_role

    # ------------------------------------------------------------------ #
    # ledger
    # ------------------------------------------------------------------ #
    def evals_path(self, asset_id: str) -> Path:
        return self.store.asset_dir(asset_id) / "evals.jsonl"

    def read_evals(self, asset_id: str) -> list[dict[str, Any]]:
        p = self.evals_path(asset_id)
        if not p.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return out

    def _append_eval_row(self, asset_id: str, row: dict[str, Any]) -> None:
        """Append one case record; prune to the retention cap atomically."""
        p = self.evals_path(asset_id)
        rows = self.read_evals(asset_id)
        rows.append(row)
        tmp = p.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")
        os.replace(tmp, p)
        if len(rows) > _EVAL_LEDGER_CAP:
            self.store.truncate_jsonl(p, _EVAL_LEDGER_CAP)

    # ------------------------------------------------------------------ #
    # scoring
    # ------------------------------------------------------------------ #
    @staticmethod
    def accuracy(rows: list[dict[str, Any]]) -> float | None:
        """Weighted accuracy over scorable rows; unknown excluded from the
        denominator (honest failure), None when nothing is scorable."""
        scored = [EvalVerdict(verdict=r["verdict"], reason="").score
                  for r in rows if r.get("verdict") in ("correct", "partial", "wrong")]
        scored = [s for s in scored if s is not None]
        return round(sum(scored) / len(scored), 3) if scored else None

    # ------------------------------------------------------------------ #
    # the pass
    # ------------------------------------------------------------------ #
    async def eval_asset(self, asset: AssetSpec, seed_key: str) -> dict[str, Any]:
        """One evaluation pass for one asset. Returns a pass summary."""
        asset_id = asset.asset_id
        cases = sample_cases(asset, self.store,
                             n=max(1, self.config.eval_cases_per_asset),
                             seed_key=seed_key)
        if cases is None:
            log_run_event(self.store, asset_id, "eval", "no_data",
                          reason="insufficient_history")
            return {"asset_id": asset_id, "skipped": True,
                    "reason": "insufficient_history"}

        rows: list[dict[str, Any]] = []
        ts = datetime.now(timezone.utc).timestamp()
        for case in cases:
            # answer from the sampled historical snapshot with the CURRENT
            # knowledge base — alerts/feedback are withheld from the answerer
            res = await self.qa.ask_with_snapshot(
                asset, case.question, case.snapshot,
                role=ModelRole.ROUTINE, session_mode="eval")
            if res.error and not res.content.strip():
                # answerer outage: no answer, no review, no fabricated score
                log_run_event(self.store, asset_id, "eval", "provider_failed",
                              reason=f"answerer: {res.error}")
                return {"asset_id": asset_id, "skipped": True,
                        "reason": f"answerer_failed: {res.error}"}
            answer = (res.content or "")[:_ANSWER_MAX_CHARS]
            gt = ground_truth_pack(asset, case.snapshot, self.store)
            verdict = await review(
                case.question, answer, gt, self.gateway,
                role=ModelRole(self.reviewer_role),
                session_key=f"eval:reviewer:{asset_id}")
            row = {
                "ts": ts,
                "asset_id": asset_id,
                "window_end": case.window_end,
                "question_kind": case.question_kind,
                "question": case.question,
                "answer": answer,
                "verdict": verdict.verdict,
                "reason": verdict.reason,
                "answer_provider": res.used_provider,
                "degraded": bool(res.degraded),
            }
            self._append_eval_row(asset_id, row)
            rows.append(row)

        acc = self.accuracy(rows)
        log_run_event(self.store, asset_id, "eval", "ok",
                      reason=f"n={len(rows)} accuracy={acc}")
        return {
            "asset_id": asset_id,
            "skipped": False,
            "n": len(rows),
            "accuracy": acc,
            "verdicts": {v: sum(1 for r in rows if r["verdict"] == v)
                         for v in ("correct", "partial", "wrong", "unknown")},
        }

    async def eval_all(self, seed_key: str | None = None,
                       asset_filter: str | None = None) -> dict[str, Any]:
        """Evaluate every enabled asset (optionally just one) and aggregate.

        A per-asset failure is isolated (logged, recorded as skipped) so one
        broken asset cannot cost the rest of the fleet their daily score.
        """
        seed_key = seed_key or eval_date_key(datetime.now(timezone.utc))
        per_asset: dict[str, Any] = {}
        all_rows: list[dict[str, Any]] = []
        for asset in self.config.assets:
            if not asset.enabled:
                continue
            if asset_filter and asset.asset_id != asset_filter:
                continue
            try:
                summary = await self.eval_asset(asset, seed_key)
            except Exception as exc:  # noqa: BLE001 — isolate per-asset failures
                logger.error("eval failed for {a}: {e}", a=asset.asset_id, e=exc)
                log_run_event(self.store, asset.asset_id, "eval", "exception",
                              reason=str(exc)[:200])
                summary = {"asset_id": asset.asset_id, "skipped": True,
                           "reason": f"exception: {exc}"}
            per_asset[asset.asset_id] = summary
            if not summary.get("skipped"):
                all_rows.extend(self.read_evals(asset.asset_id))
        return {
            "seed_key": seed_key,
            "per_asset": per_asset,
            "accuracy": self.accuracy(all_rows),
            "n": len(all_rows),
        }


async def run_eval_pass(engine: EvalEngine, asset_filter: str | None = None,
                        seed_key: str | None = None) -> dict[str, Any]:
    """Entry point shared by cron, CLI, and any manual trigger.

    The gateway must already be started by the caller (Supervisor owns its
    lifecycle; the CLI starts/stops around this call).
    """
    return await engine.eval_all(seed_key=seed_key, asset_filter=asset_filter)
