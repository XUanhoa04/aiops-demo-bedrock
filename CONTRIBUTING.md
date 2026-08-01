# Contributing

Thanks for improving SentinelLoop. Changes should solve an observable SRE
problem and preserve the safety invariants in `docs/ARCHITECTURE.md`.

## Local checks

1. Copy `.env.example` to `.env`; never commit credentials.
2. Run `docker compose config -q`.
3. Run `make ci` on Linux/macOS, or the per-service pytest commands from CI.
4. Run `python evaluation/evaluate_anomaly.py` and
   `python evaluation/evaluate_rca.py --mode offline --split all` when changing
   detection, correlation, topology, RCA, or rules.

Do not add scenario IDs, expected labels, or benchmark-only branches to
production code. New patterns belong in `config/rca_patterns.yaml` and need a
holdout or hard-suite case. Pull requests should describe operational impact,
failure behavior, rollback, and evidence used for verification.
