"""Eval sampler: stratification, reproducibility, skip rules, templates."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from oxpecker.config.model import AssetSpec
from oxpecker.core.asset_store import AssetStore
from oxpecker.eval.sampler import (
    eval_date_key,
    load_snapshot_candidates,
    sample_cases,
)


def _asset(asset_id: str = "pump1") -> AssetSpec:
    return AssetSpec(asset_id=asset_id, name="P", kind="pump")


def _snap(window_end: str, level: str, samples: int = 60) -> dict:
    return {
        "window_end": window_end,
        "overall_level": level,
        "sample_count": samples,
        "status": "ok",
    }


def _seed_store(tmp_path: Path, snaps: list[dict], asset_id: str = "pump1") -> AssetStore:
    store = AssetStore(tmp_path / "ws")
    store.ensure_asset(_asset(asset_id))
    for s in snaps:
        store.save_snapshot(asset_id, s)
    return store


def test_seed_reproducible_same_day(tmp_path: Path) -> None:
    """Same (asset, seed_key) re-run must replay the exact same sample."""
    snaps = ([_snap(f"2026-11-{d:02d}T10:00:00+00:00", "warning") for d in range(1, 10)]
             + [_snap(f"2026-11-{d:02d}T11:00:00+00:00", "info") for d in range(11, 20)])
    store = _seed_store(tmp_path, snaps)
    a = _asset()
    c1 = sample_cases(a, store, n=5, seed_key="2026-11-26")
    c2 = sample_cases(a, store, n=5, seed_key="2026-11-26")
    assert [x.window_end for x in c1] == [x.window_end for x in c2]
    # a different day samples differently (probability 1 given 18 candidates)
    c3 = sample_cases(a, store, n=5, seed_key="2026-11-27")
    assert [x.window_end for x in c1] != [x.window_end for x in c3]


def test_stratification_covers_incident_and_nominal(tmp_path: Path) -> None:
    """With both pools available, floors force coverage of each kind."""
    snaps = ([_snap(f"2026-11-{d:02d}T10:00:00+00:00", "warning") for d in range(1, 10)]
             + [_snap(f"2026-11-{d:02d}T11:00:00+00:00", "info") for d in range(11, 20)])
    store = _seed_store(tmp_path, snaps)
    cases = sample_cases(_asset(), store, n=5, seed_key="2026-11-26")
    assert cases is not None and len(cases) == 5
    kinds = {c.question_kind for c in cases}
    assert kinds == {"incident", "nominal"}
    inc = sum(1 for c in cases if c.question_kind == "incident")
    assert inc >= 1  # floor 2, n=5 permits 2-3


def test_all_nominal_archive_still_produces_cases(tmp_path: Path) -> None:
    """A fresh asset with no incidents: nominal template fills all slots."""
    snaps = [_snap(f"2026-11-{d:02d}T10:00:00+00:00", "info") for d in range(1, 8)]
    store = _seed_store(tmp_path, snaps)
    cases = sample_cases(_asset(), store, n=5, seed_key="2026-11-26")
    assert cases is not None and len(cases) == 5
    assert all(c.question_kind == "nominal" for c in cases)


def test_too_few_candidates_skips(tmp_path: Path) -> None:
    """Below the candidate floor the asset is skipped, not force-scored."""
    store = _seed_store(tmp_path, [_snap("2026-11-25T10:00:00+00:00", "warning"),
                                   _snap("2026-11-25T11:00:00+00:00", "info")])
    assert sample_cases(_asset(), store, n=5, seed_key="k") is None


def test_empty_windows_excluded(tmp_path: Path) -> None:
    """Empty/offline windows have no diagnostic content — not sampled."""
    snaps = (_snap(f"2026-11-{d:02d}T10:00:00+00:00", "warning") for d in range(1, 6))
    store = _seed_store(tmp_path, [*snaps,
                                   _snap("2026-11-25T11:00:00+00:00", "info", samples=0),
                                   _snap("2026-11-25T12:00:00+00:00", "unknown", samples=0)])
    cases = sample_cases(_asset(), store, n=4, seed_key="2026-11-26")
    assert cases is not None
    assert all(c.snapshot["sample_count"] > 0 for c in cases)


def test_question_template_matches_level(tmp_path: Path) -> None:
    """Incident windows ask for mechanism; nominal ask about early signals."""
    store = _seed_store(tmp_path, [_snap("2026-11-25T10:00:00+00:00", "critical"),
                                   _snap("2026-11-25T11:00:00+00:00", "info"),
                                   _snap("2026-11-25T12:00:00+00:00", "info")])
    cases = sample_cases(_asset(), store, n=3, seed_key="2026-11-26")
    by_kind = {c.question_kind: c for c in cases}
    assert "机理" in by_kind["incident"].question
    assert "早期信号" in by_kind["nominal"].question
    assert by_kind["incident"].window_end == "2026-11-25T10:00:00+00:00"


def test_load_candidates_ignores_corrupt_files(tmp_path: Path) -> None:
    store = _seed_store(tmp_path, [_snap("2026-11-25T10:00:00+00:00", "info"),
                                   _snap("2026-11-25T11:00:00+00:00", "info")])
    # a truncated archive (crash mid-write) must not break sampling
    corrupt = store.list_snapshots("pump1", limit=100000)[0]
    corrupt.write_text('{"window_end": "trunc', encoding="utf-8")
    cands = load_snapshot_candidates(store, "pump1")
    assert len(cands) == 1


def test_eval_date_key_utc() -> None:
    dt = datetime(2026, 11, 26, 23, 30, tzinfo=timezone.utc)
    assert eval_date_key(dt) == "2026-11-26"
