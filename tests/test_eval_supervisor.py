"""Supervisor eval-job wiring: cron firing, slot dedup, failure isolation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from oxpecker.config.model import AdviserConfig, AssetSpec, MetricSpec
from oxpecker.core.asset_store import AssetStore
from oxpecker.core.scheduler import Supervisor
from oxpecker.llm.gateway import LLMResult


def _asset() -> AssetSpec:
    return AssetSpec(asset_id="pump1", name="P", window_s=60, sample_freq_s=1.0,
                     metrics=[MetricSpec(key="temp")])


class _Gw:
    async def astart(self):
        pass

    async def aclose(self):
        pass

    async def run(self, *a, **k):
        return LLMResult(content="")


class _NoopDeps:
    def buffer_snapshot(self, *a, **k):
        pass

    async def learn_asset(self, *a, **k):
        return LLMResult(content="learned")

    async def generate(self, *a, **k):
        return LLMResult(content="# R")

    def archive(self, *a, **k):
        return Path("/tmp/r")


class StubEvalEngine:
    def __init__(self, exc: Exception | None = None):
        self.calls: list[tuple[str | None, str | None]] = []
        self.exc = exc

    async def eval_all(self, seed_key=None, asset_filter=None):
        self.calls.append((seed_key, asset_filter))
        if self.exc:
            raise self.exc
        return {"per_asset": {}, "accuracy": None, "n": 0}


def _sup(tmp_path: Path, eval_engine, eval_cron: str = "0 4 * * *") -> Supervisor:
    config = AdviserConfig(workspace=tmp_path, assets=[_asset()],
                           deep_report_cron="0 6 * * *", learn_cron="0 3 * * *",
                           eval_cron=eval_cron)
    store = AssetStore(tmp_path)
    store.save_snapshot("pump1", {
        "asset_id": "pump1", "window_end": datetime.now(timezone.utc).isoformat(),
        "status": "healthy", "overall_level": "info", "sample_count": 10,
        "coverage": 1.0, "metrics": [],
    })
    return Supervisor(config=config, store=store, gateway=_Gw(),  # type: ignore
                      report_gen=_NoopDeps(), learner=_NoopDeps(),  # type: ignore
                      eval_engine=eval_engine)


def test_eval_fires_once_per_slot(tmp_path: Path) -> None:
    engine = StubEvalEngine()
    sup = _sup(tmp_path, engine)
    asset = sup.config.assets[0]
    t = datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc)
    import asyncio
    asyncio.run(sup.run_due_jobs(asset, t))
    asyncio.run(sup.run_due_jobs(asset, t.replace(second=30)))  # same slot
    asyncio.run(sup.run_due_jobs(asset, t.replace(minute=1)))   # not matching
    asyncio.run(sup.run_due_jobs(asset, t.replace(day=27)))     # next day fires
    assert len(engine.calls) == 2
    # per-asset filter and UTC-day seed flow through
    assert engine.calls[0] == ("2026-11-26", "pump1")
    assert engine.calls[1] == ("2026-11-27", "pump1")


def test_eval_disabled_without_cron(tmp_path: Path) -> None:
    engine = StubEvalEngine()
    sup = _sup(tmp_path, engine, eval_cron="")
    asset = sup.config.assets[0]
    import asyncio
    asyncio.run(sup.run_due_jobs(asset, datetime(2026, 11, 26, 4, 0,
                                                 tzinfo=timezone.utc)))
    assert engine.calls == []


def test_eval_exception_isolated_and_audited(tmp_path: Path) -> None:
    engine = StubEvalEngine(exc=RuntimeError("boom"))
    sup = _sup(tmp_path, engine)
    asset = sup.config.assets[0]
    import asyncio
    # must not raise into the calling loop
    asyncio.run(sup.run_due_jobs(asset, datetime(2026, 11, 26, 4, 0,
                                                 tzinfo=timezone.utc)))
    assert len(engine.calls) == 1
    from oxpecker.core.runlog import read_runlog
    events = read_runlog(sup.store, "pump1", limit=10)
    assert any(e["kind"] == "eval" and e["outcome"] == "exception"
               and "boom" in e.get("reason", "") for e in events)


def test_eval_unwired_engine_is_noop(tmp_path: Path) -> None:
    """Supervisor without eval_engine (legacy embedders) must not break."""
    engine = StubEvalEngine()
    sup = _sup(tmp_path, engine)
    sup.eval_engine = None
    asset = sup.config.assets[0]
    import asyncio
    asyncio.run(sup.run_due_jobs(asset, datetime(2026, 11, 26, 4, 0,
                                                 tzinfo=timezone.utc)))
    assert engine.calls == []
