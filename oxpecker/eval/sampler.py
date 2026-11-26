"""Deterministic case sampler — picks windows and asks the questions.

Pure logic, no LLM. Every choice here (which windows, which template) is a
function of the snapshot archive and a per-day seed, so the same day re-run
produces the same cases — the evaluation itself must be reproducible before it
can measure anything else.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config.model import AssetSpec
from ..core.asset_store import AssetStore


@dataclass
class EvalCase:
    asset_id: str
    window_end: str        # the sampled snapshot's window_end (ISO)
    snapshot: dict         # full snapshot dict — both answer context and ground truth
    question: str          # template-generated diagnostic question
    question_kind: str     # "incident" | "nominal"


# stratification floors: with enough history, always cover both "something was
# wrong" and "all nominal" — an eval skewed to fault windows would overstate
# how often the knowledge base earns its keep.
_INCIDENT_FLOOR = 2
_NOMINAL_FLOOR = 2
# below this there is nothing meaningful to stratify — skip the asset rather
# than fabricate a score from a sample of one
_MIN_CANDIDATES = 3

_INCIDENT_QUESTION = (
    "根据当前数据与该设备的已知知识，该设备在 {when} 前后状态如何？"
    "哪些指标异常、可能的机理是什么？"
)
_NOMINAL_QUESTION = (
    "根据当前数据与该设备的已知知识，该设备在 {when} 时是否正常运行？"
    "有无值得关注的早期信号？"
)

_LEVELS = ("info", "warning", "critical")


def _seeded_rng(asset_id: str, seed_key: str) -> random.Random:
    """Deterministic per (asset, day) RNG so a re-run on the same day replays."""
    digest = hashlib.sha256(f"{asset_id}:{seed_key}".encode("utf-8")).hexdigest()
    return random.Random(int(digest[:8], 16))


def _question_for(snapshot: dict[str, Any]) -> tuple[str, str]:
    """Pick the question template from the snapshot's own verdict."""
    level = str(snapshot.get("overall_level", "info"))
    when = str(snapshot.get("window_end", ""))
    if _LEVELS.index(level) >= _LEVELS.index("warning"):
        return _INCIDENT_QUESTION.format(when=when), "incident"
    return _NOMINAL_QUESTION.format(when=when), "nominal"


def load_snapshot_candidates(store: AssetStore, asset_id: str) -> list[dict[str, Any]]:
    """All archived non-empty snapshots for an asset, oldest-last order."""
    out: list[dict[str, Any]] = []
    for path in store.list_snapshots(asset_id, limit=100000):
        try:
            snap = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # a truncated/unreadable archive is not a case
        # empty windows have no diagnostic content to ask about
        if int(snap.get("sample_count", 0) or 0) <= 0:
            continue
        out.append(snap)
    return out


def sample_cases(asset: AssetSpec, store: AssetStore, n: int,
                 seed_key: str) -> list[EvalCase] | None:
    """Stratified sample of n eval cases for one asset, or None to skip.

    Stratification: at least 2 incident (warning/critical) + 2 nominal (info)
    windows when the archive allows, remainder random. The seed binds to
    (asset_id, seed_key) so the same day's pass is reproducible.
    """
    candidates = load_snapshot_candidates(store, asset.asset_id)
    if len(candidates) < _MIN_CANDIDATES:
        return None

    rng = _seeded_rng(asset.asset_id, seed_key)
    incident: list[dict[str, Any]] = []
    nominal: list[dict[str, Any]] = []
    for snap in candidates:
        kind = _question_for(snap)[1]
        (incident if kind == "incident" else nominal).append(snap)

    picked: list[dict[str, Any]] = []
    # floors, capped by availability (a fresh asset may be all-nominal)
    for pool, floor in ((incident, _INCIDENT_FLOOR), (nominal, _NOMINAL_FLOOR)):
        take = min(floor, len(pool), max(0, n - len(picked)))
        if take > 0:
            picked.extend(rng.sample(pool, take))
    # remainder from whatever is left (either pool, deduped against picked)
    rest = [s for s in candidates if s not in picked]
    while len(picked) < n and rest:
        picked.append(rest.pop(rng.randrange(len(rest))))

    cases: list[EvalCase] = []
    for snap in picked:
        question, kind = _question_for(snap)
        cases.append(EvalCase(
            asset_id=asset.asset_id,
            window_end=str(snap.get("window_end", "")),
            snapshot=snap,
            question=question,
            question_kind=kind,
        ))
    return cases


def eval_date_key(now: datetime) -> str:
    """UTC date string used as the default seed key (one sample set per day)."""
    return now.strftime("%Y-%m-%d")
