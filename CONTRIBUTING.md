# Contributing to Oxpecker

Thanks for your interest in improving Oxpecker! This is an early-stage,
idea-first MVP — bug reports, use cases, and design feedback are as valuable
as code.

## Reporting issues

Open an issue with:

- What you expected vs. what happened
- Your setup: connector type (simulator/REST/MQTT/OPC UA/Modbus), Python
  version, deployment shape (local/Docker/systemd)
- For interpretation-quality problems: the snapshot JSON (from
  `<workspace>/assets/<asset>/snapshots/`) alongside the report or answer —
  the snapshot is the ground truth that lets us separate fact-layer bugs from
  model-layer ones.

## Development setup

```bash
git clone https://github.com/happy-momo/oxpecker.git
cd oxpecker
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
pip install pytest pytest-asyncio
pytest tests/ -v
```

The full suite runs without plant hardware or model accounts (simulator
connector + mock gateway).

For HMI work:

```bash
cd hmi && npm install && npm run build
# dev server with hot reload:
npm run dev
```

## Architecture ground rules

1. **Numbers come from code, never from a model.** Anything that ends up as a
   figure in a report must be computed in `oxpecker/analytics/` and stored in
   the snapshot. The LLM explains facts; it does not produce them.
2. **Gate before interpreting.** New data paths must classify through the
   trust gates (`oxpecker/analytics/gates.py`) before reaching the LLM
   gateway, and refusals must record their reason.
3. **Degrade loudly.** Provider fallbacks stamp `degraded: true` on output —
   never silently swap model tiers.
4. **Keep the nanobot dependency on public APIs.** Oxpecker does not modify
   nanobot's source and only imports its public SDK surface.
5. **Deterministic tests.** New pipeline behavior needs a test that runs
   without plant hardware (simulator + mock gateway).

## Pull requests

- Keep PRs focused; one behavior change per PR.
- Run `pytest tests/ -v` before submitting — CI blocks on failures.
- Update `CHANGELOG.md` for user-visible changes.
- For behavior changes, also update `README.md` (and
  `docs/README.zh-CN.md` when user-facing).

## License

By contributing, you agree that your contributions are licensed under the
MIT License terms of this repository.
