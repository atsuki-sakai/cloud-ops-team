# cloud-ops-team — common tasks. Run `make` to list them.
# Settings come from .env (create it with `make setup`); variables on the command line win.

.DEFAULT_GOAL := help
JOB ?= error_watch
ARGS ?=
ENVRUN := uv run --quiet python scripts/envrun.py

.PHONY: help install setup doctor jobs run chat test lint eval \
        gcp-bootstrap gcp-grant gcp-deploy gcp-schedule gcp-logs

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (uv sync)
	uv sync

setup: install ## Create .env for your project (interactive)
	uv run python scripts/setup.py

doctor: ## Check configuration, Gemini, GitHub and cloud access, with fixes
	uv run python scripts/doctor.py $(ARGS)

jobs: ## List the team's jobs and schedules
	@uv run --quiet python scripts/jobs.py

run: ## Run one job locally: make run JOB=error_watch ARGS="--hours 24"
	uv run python scripts/run_job.py $(JOB) $(ARGS)

chat: ## Talk to the team in the browser (ADK playground)
	agents-cli playground

test: ## Unit tests (offline)
	uv run pytest tests/unit -q

lint: ## Lint, format check, spell check, type check
	agents-cli lint

eval: ## Behaviour evaluation with an LLM judge (calls the model)
	agents-cli eval run

gcp-bootstrap: ## Google Cloud one-time setup: APIs, service account, roles, GitHub token secret
	$(ENVRUN) ./scripts/gcp/bootstrap.sh

gcp-grant: ## Grant the runtime service account read-only roles on OPS_TARGET
	$(ENVRUN) ./scripts/gcp/grant_target_roles.sh

gcp-deploy: ## Deploy to Cloud Run (OPS_DRY_RUN from .env; OPS_DRY_RUN=false make gcp-deploy to go live)
	$(ENVRUN) ./scripts/gcp/deploy_cloud_run.sh

gcp-schedule: ## Create or update the Cloud Scheduler jobs (ERROR_WATCH_CRON=... to change)
	$(ENVRUN) ./scripts/gcp/setup_schedules.sh

gcp-logs: ## Show the latest runs of the deployed service
	$(ENVRUN) sh -c 'gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"$${SERVICE:-cloud-ops-team}\" AND httpRequest.requestUrl:\"trigger/pubsub\"" --project "$${PROJECT_ID:-$$GOOGLE_CLOUD_PROJECT}" --limit 10 --format="table(timestamp,httpRequest.status,httpRequest.latency)"'
