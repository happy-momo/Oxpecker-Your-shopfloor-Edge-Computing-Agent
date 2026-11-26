"""LLM reviewer — scores an eval answer against deterministic ground truth.

The ground-truth pack is assembled from files already on disk (the snapshot's
own verdict, the alerts ledger, the feedback ledger); every number in it was
computed by the analytics engine or recorded by an operator, never generated.
The reviewer LLM only decides whether the candidate answer is consistent with
that pack — it never sees the knowledge base or the answerer's provider, so it
cannot grade the system it belongs to by sympathy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config.model import AlertLevel, AssetSpec, ModelRole
from ..core.asset_store import AssetStore
from ..llm.gateway import LLMGateway
from .prompts import REVIEW_PROMPT

# verdict vocabulary — anything else the reviewer returns is treated as a
# failed review (unknown), never silently coerced into a score
VALID_VERDICTS = ("correct", "partial", "wrong", "unknown")

_SCORES = {"correct": 1.0, "partial": 0.5, "wrong": 0.0, "unknown": None}


@dataclass
class EvalVerdict:
    verdict: str
    reason: str

    @property
    def score(self) -> float | None:
        """Numeric score; None (unknown) is excluded from the accuracy denominator."""
        return _SCORES.get(self.verdict)


def _parse_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    return None


def _in_band(v: float | None, lo: float | None, hi: float | None) -> bool:
    if v is None:
        return False
    return not ((lo is not None and v < lo) or (hi is not None and v > hi))


def _metrics_in_band(snap: dict[str, Any], asset: AssetSpec,
                     band: str) -> list[dict[str, Any]]:
    """Metrics whose level (computed by the engine, re-checked against the spec's
    band here for a compact ground-truth listing) sits in warn/crit."""
    out: list[dict[str, Any]] = []
    for m in snap.get("metrics") or []:
        spec = asset.metric(str(m.get("key", "")))
        if spec is None:
            continue
        probe = m.get("mean")
        lo = spec.crit_min if band == "crit" else spec.warn_min
        hi = spec.crit_max if band == "crit" else spec.warn_max
        exceed_key = "over_crit_runs" if band == "crit" else "over_warn_runs"
        under_key = "under_crit_runs" if band == "crit" else "under_warn_runs"
        exceeded = int(m.get(exceed_key) or 0) > 0 or int(m.get(under_key) or 0) > 0
        # primary signal is the engine's own exceedance record; the mean-vs-band
        # check only catches persistent-outside-band shapes with no recorded run
        if exceeded or (probe is not None and not _in_band(probe, lo, hi)):
            out.append({
                "key": m.get("key"),
                "mean": m.get("mean"),
                "max": m.get("max"),
                "min": m.get("min"),
                "unit": m.get("unit", ""),
                "level": m.get("level", "info"),
            })
    return out


def _trends(snap: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in snap.get("metrics") or []:
        slope = m.get("slope")
        if isinstance(slope, (int, float)) and abs(slope) > 0:
            out.append({"key": m.get("key"), "slope_per_hour": round(slope, 4),
                        "r2": m.get("slope_r2")})
    return out


def ground_truth_pack(asset: AssetSpec, snap: dict[str, Any], store: AssetStore,
                      *, now: datetime | None = None,
                      alerts_window_min: int = 15,
                      feedback_after_h: int = 24) -> dict[str, Any]:
    """Deterministic reference material for one evaluated window.

    Alerts within ±window of the snapshot; operator feedback recorded within
    feedback_after_h *after* the window (what actually happened next).
    """
    now = now or datetime.now(timezone.utc)
    pack: dict[str, Any] = {
        "snapshot_verdict": {
            "overall_level": snap.get("overall_level", "info"),
            "trust": snap.get("trust", ""),
            "status": snap.get("status", ""),
            "metrics_in_warn": _metrics_in_band(snap, asset, "warn"),
            "metrics_in_crit": _metrics_in_band(snap, asset, "crit"),
            "trends": _trends(snap),
            "headline": snap.get("headline", ""),
        },
        "alerts_in_window": [],
        "feedback_after": [],
    }
    end_ts = _parse_epoch(snap.get("window_end"))
    if end_ts is not None:
        lo, hi = end_ts - alerts_window_min * 60, end_ts + alerts_window_min * 60
        for a in store.read_alerts(asset.asset_id):
            ts = _parse_epoch(a.get("ts"))
            if ts is not None and lo <= ts <= hi:
                pack["alerts_in_window"].append({
                    "ts": a.get("ts"),
                    "subject": a.get("subject", ""),
                    "headline": a.get("headline", ""),
                })
        fb_hi = end_ts + feedback_after_h * 3600
        for f in store.read_feedback(asset.asset_id):
            ts = _parse_epoch(f.get("ts"))
            if ts is not None and end_ts <= ts <= fb_hi:
                pack["feedback_after"].append({"ts": f.get("ts"),
                                               "note": f.get("note", "")})
    return pack


def parse_verdict(content: str) -> EvalVerdict | None:
    """Defensive parse of the reviewer's JSON verdict.

    Reuses the gateway's fence-stripping + balanced-brace extraction via
    json attempts here; any non-conforming payload (missing fields, invalid
    verdict word, prose) yields None → the caller records `unknown`.
    """
    text = (content or "").strip()
    if not text:
        return None
    # strip a possible code fence
    if text.startswith("```"):
        first_nl = text.find("\n")
        end_fence = text.rfind("```")
        if first_nl >= 0 and end_fence > first_nl:
            text = text[first_nl + 1:end_fence].strip()
    obj = None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            obj = json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            obj = None
                        break
    if not isinstance(obj, dict):
        return None
    verdict = str(obj.get("verdict", "")).strip().lower()
    if verdict not in VALID_VERDICTS:
        return None
    return EvalVerdict(verdict=verdict, reason=str(obj.get("reason", ""))[:500])


async def review(case_question: str, answer: str, gt_pack: dict[str, Any],
                 gateway: LLMGateway, *, role: ModelRole = ModelRole.ROUTINE,
                 session_key: str = "eval:reviewer") -> EvalVerdict:
    """One reviewer call. Provider failure or unparseable output → unknown.

    unknown is honest: the case still lands in the ledger (with its answer for
    later inspection) but does not enter the accuracy denominator.
    """
    prompt = REVIEW_PROMPT.format(
        question=case_question,
        answer=answer or "(no answer produced)",
        reference=json.dumps(gt_pack, ensure_ascii=False, indent=2),
    )
    try:
        res = await gateway.run(prompt, role=role, session_key=session_key,
                                parse_json=True)
    except Exception as exc:  # noqa: BLE001 — a reviewer outage must not kill the pass
        return EvalVerdict(verdict="unknown", reason=f"reviewer error: {exc}")
    if res.error and not res.content.strip():
        return EvalVerdict(verdict="unknown", reason=f"reviewer failed: {res.error}")
    parsed = parse_verdict(res.content)
    if parsed is None:
        return EvalVerdict(verdict="unknown",
                           reason=f"reviewer output unparseable: {res.content[:200]}")
    return parsed
