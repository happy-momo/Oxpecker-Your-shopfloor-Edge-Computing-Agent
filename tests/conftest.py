"""Test-path bootstrap: make `oxpecker` (the inner package) importable.

`oxpecker/tests/` sits inside the oxpecker *subproject* directory whose
own package is `oxpecker/oxpecker/`. pytest's rootdir insertion does not
cover it (the repo root is the nanobot package), so every test module previously
bootstrapped `sys.path` by hand — and only when it remembered. One conftest,
one rule: PYTHONPATH includes the subproject root before any module imports.
"""
from pathlib import Path
import sys

_SUBPROJECT = Path(__file__).resolve().parent.parent   # oxpecker/
if str(_SUBPROJECT) not in sys.path:
    sys.path.insert(0, str(_SUBPROJECT))
