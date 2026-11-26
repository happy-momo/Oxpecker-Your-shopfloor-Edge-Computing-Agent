"""/api/stats evaluation block: aggregation, unknown exclusion, disabled shape."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from oxpecker.config.model import AdviserConfig
from oxpecker.core.asset_store import AssetStore
from oxpecker.server.app import create_app


class StubGateway:
    async def astart(self) -> None:
        pass

    async def aclose(self) -> None:
        pass


def _client(tmp_path: Path, eval_cron: str = "0 4 * * *") -> TestClient:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({
        "workspace": str(tmp_path / "ws"),
        "assets": [{"asset_id": "pump1", "metrics": []}],
        "sources": [],
        "eval_cron": eval_cron,
        "hmi_pin": "none",
    }), encoding="utf-8")
    return TestClient(create_app(config_path, gateway=StubGateway()))


def _seed_evals(tmp_path: Path, rows: list[dict]) -> None:
    from oxpecker.config.model import AssetSpec
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(AssetSpec(asset_id="pump1", name="P"))
    p = store.asset_dir("pump1") / "evals.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_stats_evaluation_disabled_shape(tmp_path: Path) -> None:
    c = _client(tmp_path, eval_cron="")
    d = c.get("/api/stats").json()["data"]
    assert d["evaluation"] == {"enabled": False}


def test_stats_evaluation_empty_enabled(tmp_path: Path) -> None:
    """Enabled but no ledger yet: honest nulls, not fabricated zeros."""
    c = _client(tmp_path)
    d = c.get("/api/stats").json()["data"]
    ev = d["evaluation"]
    assert ev["enabled"] is True
    assert ev["last_run_ts"] is None and ev["last_run_accuracy"] is None
    assert ev["accuracy_7d"] == [] and ev["per_asset"] == {}


def test_stats_evaluation_aggregation(tmp_path: Path) -> None:
    t = time.time()
    rows = [
        {"ts": t, "verdict": "correct", "reason": "r"},
        {"ts": t, "verdict": "partial", "reason": "r"},
        {"ts": t, "verdict": "wrong", "reason": "r"},
        {"ts": t, "verdict": "unknown", "reason": "reviewer down"},
    ]
    _seed_evals(tmp_path, rows)
    c = _client(tmp_path)
    ev = c.get("/api/stats").json()["data"]["evaluation"]
    # unknown excluded from denominator: (1.0 + 0.5 + 0.0) / 3
    assert ev["last_run_accuracy"] == 0.5
    assert ev["verdict_counts"] == {"correct": 1, "partial": 1, "wrong": 1,
                                    "unknown": 1}
    assert ev["per_asset"]["pump1"]["n"] == 4
    assert ev["per_asset"]["pump1"]["accuracy"] == 0.5
    point = ev["accuracy_7d"][-1]
    assert point["n"] == 4 and point["correct"] == 1 and point["accuracy"] == 0.5


def test_stats_evaluation_multiday_trend(tmp_path: Path) -> None:
    day = 24 * 3600
    now = time.time()
    rows = [
        {"ts": now - 6 * day, "verdict": "wrong", "reason": ""},     # 6 days ago
        {"ts": now, "verdict": "correct", "reason": ""},             # today
    ]
    _seed_evals(tmp_path, rows)
    c = _client(tmp_path)
    ev = c.get("/api/stats").json()["data"]["evaluation"]
    assert len(ev["accuracy_7d"]) == 2
    assert ev["accuracy_7d"][0]["accuracy"] == 0.0
    assert ev["accuracy_7d"][1]["accuracy"] == 1.0
    # improvement across the window is exactly the "getting better" signal
    assert ev["accuracy_7d"][1]["accuracy"] > ev["accuracy_7d"][0]["accuracy"]
