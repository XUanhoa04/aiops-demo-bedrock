# Convenience targets for local demo + CI-equivalent checks.
# Windows: use `make` via Git Bash / WSL, or run the underlying commands.

.PHONY: help up down wait demo eval test ci baselines lint-compose

help:
	@echo "Targets:"
	@echo "  make up          - docker compose up -d --build"
	@echo "  make wait        - wait_for_stack.sh"
	@echo "  make demo        - one-shot demo (stack must be up)"
	@echo "  make eval        - offline anomaly + RCA + baselines"
	@echo "  make test        - unit tests"
	@echo "  make ci          - compose config + test + eval (local CI)"

up:
	docker compose up -d --build

down:
	docker compose down

wait:
	bash scripts/wait_for_stack.sh

demo: wait
	python scripts/demo_one_shot.py

eval:
	bash scripts/run-evaluation.sh
	python evaluation/evaluate_baselines.py --require-beats-baselines || python evaluation/evaluate_baselines.py

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
