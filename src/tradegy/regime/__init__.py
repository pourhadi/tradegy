"""Regime classification subsystem (Phases 2-3 of the regime-gated
range-scalp plan).

Two-stage pipeline:

  Phase 2 — LLM session labeling. For each historical trading session,
  call Claude with a sealed pre-open snapshot (no data after 09:30 ET
  that day) and have it emit a structured `SessionLabel`: regime
  classification + per-event impact assessment + reasoning. Ensemble
  over N calls per session for stability. Output is JSONL per
  instrument under `data/session_labels/`.

  Phase 3 — Distillation classifier. Train a deterministic gradient-
  boosted tree on Phase 1 numeric features → Phase 2 LLM labels.
  Walk-forward training with a held-out trailing window. Output
  features (`<inst>_regime_label_predicted`, `<inst>_regime_confidence`)
  flow through the existing feature pipeline; the deterministic
  classifier is what the strategy class consumes at backtest time.

The discipline justification (per plan): walk-forward / CPCV gates
require that strategy code did not see OOS data. Frontier LLMs have
seen everything in their training cutoff window, so we cannot use
the LLM in the strategy-evaluation path. The distillation classifier
is the workaround — the LLM was a teacher during development; the
deployed model is fully deterministic and gradient-validated.
"""
