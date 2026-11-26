"""Eval reviewer: ground-truth pack assembly, verdict parsing, failure paths."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from oxpecker.config.model import AssetSpec, MetricSpec, ModelRole
from oxpecker.core.asset_store import AssetStore
from oxpecker.eval.reviewer import (
    EvalVerdict,
    ground_truth_pack,
    parse_verdict,
    review,
)


def _asset() -> AssetSpec:
    return AssetSpec(
        asset_id="pump1", name="P", kind="pump",
        metrics=[
            MetricSpec(key="temp", unit="degC", warn_max=68, crit_max=73),
            MetricSpec(key="vibration", unit="mm/s", warn_max=3.0, crit_max=4.5),
        ],
    )


def _snap() -> dict:
    return {
        "window_start": "2026-11-25T14:30:00+00:00",
        "window_end": "2026-11-25T14:32:00+00:00",
        "overall_level": "warning",
        "trust": "full",
        "status": "healthy",
        "headline": "temp over warn band",
        "metrics": [
            {"key": "temp", "unit": "degC", "n": 60, "mean": 70.5, "max": 72.1,
             "min": 65.0, "level": "warning", "over_warn_runs": 2,
             "under_warn_runs": 0, "over_crit_runs": 0, "under_crit_runs": 0,
             "slope": 1.8, "slope_r2": 0.9},
            {"key": "vibration", "unit": "mm/s", "n": 60, "mean": 2.1, "max": 2.4,
             "min": 1.8, "level": "info", "over_warn_runs": 0,
             "under_warn_runs": 0, "over_crit_runs": 0, "under_crit_runs": 0,
             "slope": 0.0},
        ],
        "sample_count": 120,
    }


# --------------------------------------------------------------------------- #
# ground_truth_pack
# --------------------------------------------------------------------------- #
def test_gt_pack_classifies_bands(tmp_path) -> None:
    store = AssetStore(tmp_path / "ws")
    pack = ground_truth_pack(_asset(), _snap(), store)
    sv = pack["snapshot_verdict"]
    assert sv["overall_level"] == "warning"
    assert [m["key"] for m in sv["metrics_in_warn"]] == ["temp"]
    assert sv["metrics_in_crit"] == []
    assert sv["trends"] == [{"key": "temp", "slope_per_hour": 1.8, "r2": 0.9}]


def test_gt_pack_filters_alerts_by_window(tmp_path) -> None:
    store = AssetStore(tmp_path / "ws")
    end = datetime.fromisoformat("2026-11-25T14:32:00+00:00").timestamp()
    rows = [
        {"ts": end - 600, "subject": "temp", "headline": "in window"},     # -10min
        {"ts": end + 700, "subject": "temp", "headline": "in window"},     # +11min
        {"ts": end - 1800, "subject": "temp", "headline": "too early"},    # -30min
        {"ts": end + 2000, "subject": "temp", "headline": "too late"},     # +33min
    ]
    p = store.alerts_path("pump1")
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    pack = ground_truth_pack(_asset(), _snap(), store, alerts_window_min=15)
    heads = [a["headline"] for a in pack["alerts_in_window"]]
    assert heads == ["in window", "in window"]


def test_gt_pack_filters_feedback_after_window(tmp_path) -> None:
    store = AssetStore(tmp_path / "ws")
    end = datetime.fromisoformat("2026-11-25T14:32:00+00:00").timestamp()
    rows = [
        {"ts": end - 100, "note": "before window — excluded"},
        {"ts": end + 3600, "note": "bearing replaced"},
        {"ts": end + 30 * 3600, "note": "beyond 24h — excluded"},
    ]
    p = store.feedback_path("pump1")
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    pack = ground_truth_pack(_asset(), _snap(), store)
    notes = [f["note"] for f in pack["feedback_after"]]
    assert notes == ["bearing replaced"]


# --------------------------------------------------------------------------- #
# parse_verdict
# --------------------------------------------------------------------------- #
def test_parse_verdict_plain_json() -> None:
    v = parse_verdict('{"verdict": "correct", "reason": "matches snapshot"}')
    assert v is not None and v.verdict == "correct"
    assert v.score == 1.0


def test_parse_verdict_fenced_and_prose_wrapped() -> None:
    fenced = '```json\n{"verdict": "partial", "reason": "missed trend"}\n```'
    v = parse_verdict(fenced)
    assert v is not None and v.verdict == "partial" and v.score == 0.5
    prose = 'Sure. {"verdict": "wrong", "reason": "contradicts alert"} hope this helps'
    v2 = parse_verdict(prose)
    assert v2 is not None and v2.verdict == "wrong" and v2.score == 0.0


def test_parse_verdict_invalid_and_garbage() -> None:
    # unknown is a legal verdict word (excluded from denominator)
    v = parse_verdict('{"verdict": "unknown", "reason": "material thin"}')
    assert v is not None and v.verdict == "unknown" and v.score is None
    # invalid vocabulary, missing field, prose only — all fail to parse
    assert parse_verdict('{"verdict": "excellent", "reason": "x"}') is None
    assert parse_verdict('{"reason": "no verdict"}') is None
    assert parse_verdict("I think it is fine.") is None
    assert parse_verdict("") is None


# --------------------------------------------------------------------------- #
# review (gateway interaction)
# --------------------------------------------------------------------------- #
class StubGateway:
    def __init__(self, content: str = "", error: str | None = None,
                 exc: Exception | None = None):
        self.content = content
        self.error = error
        self.exc = exc
        self.calls: list[dict] = []

    async def run(self, prompt, *, role=ModelRole.ROUTINE, session_key="",
                  blocks=None, parse_json=False, model_preset=None,
                  attributes=None):
        self.calls.append({"prompt": prompt, "role": role,
                           "session_key": session_key})
        if self.exc:
            raise self.exc
        from oxpecker.llm.gateway import LLMResult
        return LLMResult(content=self.content, error=self.error,
                         used_provider="stub", used_role=role)


def test_review_success_path() -> None:
    gw = StubGateway(content='{"verdict": "correct", "reason": "ok"}')
    v = review("q", "a", {"snapshot_verdict": {}}, gw)
    import asyncio
    v = asyncio.run(v)
    assert v.verdict == "correct"
    # reviewer prompt must NOT contain knowledge-base or provider identity leaks:
    # it sees only question + answer + reference material
    sent = gw.calls[0]["prompt"]
    assert "MEMORY" not in sent


def test_review_provider_error_is_unknown() -> None:
    import asyncio
    gw = StubGateway(error="provider down")
    v = asyncio.run(review("q", "a", {}, gw))
    assert v.verdict == "unknown" and "provider down" in v.reason


def test_review_exception_is_unknown() -> None:
    import asyncio
    gw = StubGateway(exc=RuntimeError("boom"))
    v = asyncio.run(review("q", "a", {}, gw))
    assert v.verdict == "unknown" and "boom" in v.reason


def test_review_unparseable_output_is_unknown() -> None:
    import asyncio
    gw = StubGateway(content="the answer looks fine to me")
    v = asyncio.run(review("q", "a", {}, gw))
    assert v.verdict == "unknown" and "unparseable" in v.reason
