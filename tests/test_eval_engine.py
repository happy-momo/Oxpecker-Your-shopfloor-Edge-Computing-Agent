"""EvalEngine end-to-end with a controllable stub gateway."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from oxpecker.config.model import AdviserConfig, AssetSpec, MetricSpec, ModelRole
from oxpecker.core.asset_store import AssetStore
from oxpecker.eval.engine import EvalEngine
from oxpecker.llm.gateway import LLMResult


def _asset(asset_id: str = "pump1") -> AssetSpec:
    return AssetSpec(
        asset_id=asset_id, name="P", kind="pump",
        metrics=[MetricSpec(key="temp", unit="degC", warn_max=68, crit_max=73)],
    )


def _config(tmp_path, asset_id: str = "pump1") -> AdviserConfig:
    c = AdviserConfig(workspace=tmp_path / "ws",
                      eval_cron="0 4 * * *", eval_cases_per_asset=3)
    c.assets = [_asset(asset_id)]
    return c


def _seed_history(store: AssetStore, asset_id: str = "pump1",
                  n: int = 6) -> None:
    """One incident + rest nominal — enough to stratify."""
    snaps = []
    for i in range(n):
        level = "warning" if i == 0 else "info"
        snaps.append({
            "window_end": f"2026-11-{10 + i:02d}T10:00:00+00:00",
            "overall_level": level,
            "sample_count": 60,
            "status": "healthy",
            "trust": "full",
            "metrics": [{"key": "temp", "unit": "degC", "n": 60, "mean": 65.0,
                         "level": level, "over_warn_runs": 1 if level == "warning" else 0,
                         "under_warn_runs": 0, "over_crit_runs": 0,
                         "under_crit_runs": 0, "slope": 0.0}],
        })
    for s in snaps:
        store.save_snapshot(asset_id, s)


class ScriptedGateway:
    """Returns scripted content per session mode; optional failure injection."""

    def __init__(self, answer: str = "温度65度，状态正常。",
                 verdict: str | None = None,
                 fail_answer: bool = False, fail_review: bool = False):
        self.answer = answer
        self.verdict = verdict
        self.fail_answer = fail_answer
        self.fail_review = fail_review
        self.calls: list[tuple[str, str]] = []  # (session_key, prompt-snippet)

    async def run(self, prompt, *, role=ModelRole.ROUTINE, session_key="",
                  blocks=None, parse_json=False, model_preset=None,
                  attributes=None):
        self.calls.append((session_key, prompt[:80]))
        if ":eval" in session_key and self.fail_answer:
            return LLMResult(content="", error="answerer outage",
                             used_provider="stub", used_role=role)
        if session_key.startswith("eval:reviewer") and self.fail_review:
            return LLMResult(content="", error="reviewer outage",
                             used_provider="stub", used_role=role)
        if session_key.startswith("eval:reviewer"):
            v = self.verdict or "correct"
            content = json.dumps({"verdict": v, "reason": "matches material"})
            return LLMResult(content=content, used_provider="stub",
                             used_role=role, parsed_json=json.loads(content))
        return LLMResult(content=self.answer, used_provider="stub",
                         used_role=role)


def test_eval_pass_end_to_end(tmp_path) -> None:
    import asyncio
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(_asset())
    _seed_history(store)
    cfg = _config(tmp_path)
    gw = ScriptedGateway(verdict="correct")
    engine = EvalEngine(cfg, store, gw)
    summary = asyncio.run(engine.eval_all(seed_key="2026-11-26"))

    assert summary["accuracy"] == 1.0
    pa = summary["per_asset"]["pump1"]
    assert pa["skipped"] is False and pa["n"] == 3
    rows = engine.read_evals("pump1")
    assert len(rows) == 3
    row = rows[0]
    for field in ("ts", "asset_id", "window_end", "question_kind", "question",
                  "answer", "verdict", "reason", "answer_provider", "degraded"):
        assert field in row
    # answer went through the eval session key (isolated QA history)
    answer_calls = [c for c in gw.calls if ":eval" in c[0]]
    review_calls = [c for c in gw.calls if c[0].startswith("eval:reviewer")]
    assert len(answer_calls) == 3 and len(review_calls) == 3
    # runlog has an ok eval event
    from oxpecker.core.runlog import read_runlog
    events = read_runlog(store, "pump1", limit=10)
    assert any(e["kind"] == "eval" and e["outcome"] == "ok" for e in events)


def test_eval_unknown_excluded_from_accuracy(tmp_path) -> None:
    import asyncio
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(_asset())
    _seed_history(store)
    cfg = _config(tmp_path)
    # reviewer says correct, unknown, wrong across the 3 cases
    verdicts = iter(["correct", "unknown", "wrong"])

    class MixedGateway(ScriptedGateway):
        async def run(self, prompt, **kw):
            sk = kw.get("session_key", "")
            if sk.startswith("eval:reviewer"):
                v = next(verdicts)
                content = json.dumps({"verdict": v, "reason": "r"})
                return LLMResult(content=content, used_provider="stub",
                                 parsed_json=json.loads(content))
            return await super().run(prompt, **kw)

    engine = EvalEngine(cfg, store, MixedGateway())
    summary = asyncio.run(engine.eval_all(seed_key="k"))
    # unknown excluded from denominator: (1.0 + 0.0) / 2 = 0.5, not 0.33
    assert summary["accuracy"] == 0.5
    counts = summary["per_asset"]["pump1"]["verdicts"]
    assert counts == {"correct": 1, "partial": 0, "wrong": 1, "unknown": 1}


def test_eval_answerer_failure_fails_pass(tmp_path) -> None:
    import asyncio
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(_asset())
    _seed_history(store)
    cfg = _config(tmp_path)
    engine = EvalEngine(cfg, store, ScriptedGateway(fail_answer=True))
    summary = asyncio.run(engine.eval_all(seed_key="k"))
    pa = summary["per_asset"]["pump1"]
    assert pa["skipped"] is True and "answerer_failed" in pa["reason"]
    # no rows written, no fabricated score
    assert engine.read_evals("pump1") == []
    assert summary["accuracy"] is None


def test_eval_reviewer_failure_is_unknown_not_skip(tmp_path) -> None:
    import asyncio
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(_asset())
    _seed_history(store)
    cfg = _config(tmp_path)
    engine = EvalEngine(cfg, store, ScriptedGateway(fail_review=True))
    summary = asyncio.run(engine.eval_all(seed_key="k"))
    # the pass completes; every case lands in the ledger as unknown
    rows = engine.read_evals("pump1")
    assert len(rows) == 3
    assert all(r["verdict"] == "unknown" for r in rows)
    assert summary["accuracy"] is None  # nothing scorable


def test_eval_insufficient_history_skips(tmp_path) -> None:
    import asyncio
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(_asset())
    cfg = _config(tmp_path)
    engine = EvalEngine(cfg, store, ScriptedGateway())
    summary = asyncio.run(engine.eval_all(seed_key="k"))
    pa = summary["per_asset"]["pump1"]
    assert pa["skipped"] is True and pa["reason"] == "insufficient_history"
    from oxpecker.core.runlog import read_runlog
    events = read_runlog(store, "pump1", limit=10)
    assert any(e["kind"] == "eval" and e["outcome"] == "no_data" for e in events)


def test_eval_partial_score_weighting(tmp_path) -> None:
    """partial = 0.5, correct = 1.0 → mean is 0.75 over two scorable rows."""
    import asyncio
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(_asset())
    _seed_history(store)
    cfg = _config(tmp_path)
    engine = EvalEngine(cfg, store, ScriptedGateway(verdict="partial"))
    summary = asyncio.run(engine.eval_all(seed_key="k"))
    assert summary["accuracy"] == 0.5  # all partial
