"""Retrospective evaluation loop — measures whether learned knowledge helps.

The loop samples historical snapshot windows (deterministically), answers a
diagnostic question about each using the *current* MEMORY.md knowledge (the
answerer never sees alerts or operator feedback), and has an LLM reviewer score
the answer against a ground-truth pack assembled from deterministic sources
(snapshot verdict + contemporaneous alerts + subsequent feedback). Accuracy
statistics land in /api/stats — "the longer it runs, the better it explains"
becomes a measurable claim instead of a belief.
"""
