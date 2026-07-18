# Convenience targets for local demo + CI-equivalent checks.
# Windows: use `make` via Git Bash / WSL, or run the underlying commands with py -3.11.

.PHONY: help up down wait demo astronomy astronomy-stop eval eval-compare eval-live test ci baselines lint-compose report

help:
	@echo "Targets:"
	@echo "  make up              - docker compose up -d --build (mini apps)"
	@echo "  make wait            - wait_for_stack.sh"
	@echo "  make demo            - one-shot demo (stack must be up)"
	@echo "  make astronomy       - start AIOps + OpenTelemetry Demo (Astronomy Shop)"
	@echo "  make astronomy-stop  - stop Astronomy Shop only"
	@echo "  make eval            - offline anomaly + RCA + baselines + summary"
	@echo "  make eval-compare    - offline eval + rule vs Bedrock compare"
	@echo "  make eval-live       - live e2e (stack up): chaos → RCA score"
	@echo "  make report          - print evaluation/results summary"
	@echo "  make test            - unit tests"
	@echo "  make ci              - compose config + test + eval"

up:
	docker compose up -d --build

down:
	docker compose down

wait:
	bash scripts/wait_for_stack.sh

demo: wait
	python scripts/demo_one_shot.py

astronomy:
	@echo "Starting Astronomy Shop bridge (see docs/OTEL_DEMO.md)..."
	bash scripts/astronomy/start.sh || powershell -ExecutionPolicy Bypass -File scripts/astronomy/start.ps1

astronomy-stop:
	bash scripts/astronomy/stop.sh || powershell -ExecutionPolicy Bypass -File scripts/astronomy/stop.ps1

eval:
	bash scripts/run-evaluation.sh

eval-compare:
	bash scripts/run-evaluation.sh --compare

eval-live:
	python evaluation/evaluate_live_e2e.py --limit 5 --split core
	python evaluation/report_summary.py

report:
	python evaluation/report_summary.py

baselines:
	python evaluation/evaluate_baselines.py --require-beats-baselines

test:
	PYTHONPATH=shared:aiops-services/anomaly-detector pytest -q aiops-services/anomaly-detector/tests
	PYTHONPATH=shared:aiops-services/decision-engine pytest -q aiops-services/decision-engine/tests
	PYTHONPATH=shared:aiops-services/engine-qa pytest -q aiops-services/engine-qa/tests
	PYTHONPATH=shared:aiops-services/incident-manager pytest -q aiops-services/incident-manager/tests
	PYTHONPATH=shared:aiops-services/rca-engine pytest -q aiops-services/rca-engine/tests
	PYTHONPATH=shared:aiops-services/remediation pytest -q aiops-services/remediation/tests

lint-compose:
	docker compose config -q

ci: lint-compose test eval
	@echo "Local CI checks passed."
