# Grove Odoocker — Makefile
# Usage: make <target> [env=sandbox|production] [CONFIRM=yes]

.DEFAULT_GOAL := help

# ── Release ──────────────────────────────────────────────────────────────────

## release-prepare version=vX.Y.Z  — Create a local annotated tag ready to push
.PHONY: release-prepare
release-prepare:
	@if [ -z "$(version)" ]; then \
		echo "Usage: make release-prepare version=vX.Y.Z"; \
		exit 1; \
	fi
	@if ! echo "$(version)" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+$$'; then \
		echo "Version must match vX.Y.Z (e.g. v1.2.3)"; \
		exit 1; \
	fi
	git fetch --tags
	@if git rev-parse $(version) >/dev/null 2>&1; then \
		echo "Tag $(version) already exists. Use a new version."; \
		exit 1; \
	fi
	git tag -a $(version) -m "Release $(version)"
	@echo ""
	@echo "Tag $(version) created locally."
	@echo "Push with: git push origin $(version)"
	@echo ""
	@echo "This will trigger the Release workflow:"
	@echo "  verify-image → (sandbox-smoke) → require-approval → deploy-production → post-deploy-smoke → notify"

# ── Terraform ────────────────────────────────────────────────────────────────
# Set env= on the CLI: make tf-plan env=sandbox
TF_DIR ?= infra/terraform/environments/$(env)

## tf-init env=sandbox|production  — Initialize the Terraform backend
.PHONY: tf-init
tf-init:
	terraform -chdir=$(TF_DIR) init -backend-config=backend.hcl

## tf-plan env=sandbox|production  — Show the planned infrastructure changes
.PHONY: tf-plan
tf-plan:
	terraform -chdir=$(TF_DIR) plan

## tf-apply env=sandbox|production [CONFIRM=yes]  — Apply changes (CONFIRM=yes required for production)
.PHONY: tf-apply
tf-apply:
	@if [ "$(env)" = "production" ] && [ "$(CONFIRM)" != "yes" ]; then \
		echo "Refusing to apply to production without CONFIRM=yes"; exit 1; fi
	terraform -chdir=$(TF_DIR) apply -auto-approve

## tf-destroy env=sandbox|production [CONFIRM=yes]  — Tear down (CONFIRM=yes required for production)
.PHONY: tf-destroy
tf-destroy:
	@if [ "$(env)" = "production" ] && [ "$(CONFIRM)" != "yes" ]; then \
		echo "Refusing to destroy production without CONFIRM=yes"; exit 1; fi
	terraform -chdir=$(TF_DIR) destroy -auto-approve

## tf-output env=sandbox|production  — Show outputs from the last apply
.PHONY: tf-output
tf-output:
	terraform -chdir=$(TF_DIR) output

## tf-fmt env=sandbox|production  — Recursively format Terraform files
.PHONY: tf-fmt
tf-fmt:
	terraform -chdir=$(TF_DIR) fmt -recursive

## tf-validate env=sandbox|production  — Validate config without touching the backend
.PHONY: tf-validate
tf-validate:
	terraform -chdir=$(TF_DIR) init -backend=false && terraform -chdir=$(TF_DIR) validate

# ── State-backend (special: bootstraps the grove-tf-state bucket itself) ─────
# Uses LOCAL Terraform backend so it can run before any other env exists.
# Provider credentials flow from 1Password via `op run --env-file=.env.op`
# so they never enter shell scrollback. See README.md in the env dir.
STATE_BACKEND_DIR := infra/terraform/environments/state-backend

## state-backend-init  — Initialize the local TF backend for state-backend
.PHONY: state-backend-init
state-backend-init:
	terraform -chdir=$(STATE_BACKEND_DIR) init

## state-backend-validate  — Validate state-backend config without touching the backend
.PHONY: state-backend-validate
state-backend-validate:
	terraform -chdir=$(STATE_BACKEND_DIR) init -backend=false && terraform -chdir=$(STATE_BACKEND_DIR) validate

## state-backend-plan  — Show planned changes (creds from 1Password via op run)
.PHONY: state-backend-plan
state-backend-plan:
	op run --env-file=$(STATE_BACKEND_DIR)/.env.op -- terraform -chdir=$(STATE_BACKEND_DIR) plan

## state-backend-apply  — Provision grove-tf-state bucket + Spaces key + GH secrets
.PHONY: state-backend-apply
state-backend-apply:
	op run --env-file=$(STATE_BACKEND_DIR)/.env.op -- terraform -chdir=$(STATE_BACKEND_DIR) apply -auto-approve

## state-backend-output  — Show outputs (bucket name, endpoint, synced GH secret names)
.PHONY: state-backend-output
state-backend-output:
	terraform -chdir=$(STATE_BACKEND_DIR) output

## state-backend-destroy CONFIRM=yes  — Tear down. WARNING: wipes all envs' TF state.
.PHONY: state-backend-destroy
state-backend-destroy:
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "Refusing to destroy state-backend without CONFIRM=yes"; \
		echo "WARNING: destroying grove-tf-state invalidates the state of bootstrap, sandbox, and production envs."; \
		echo "You will also need to remove the prevent_destroy lifecycle block first — see README."; \
		exit 1; fi
	op run --env-file=$(STATE_BACKEND_DIR)/.env.op -- terraform -chdir=$(STATE_BACKEND_DIR) destroy

# ── QA (monolith) — RETIRED 2026-07-04 ──────────────────────────────────────
# The monolith QA droplet stack (TF env, deploy pipeline, fast-iteration SSH
# helpers) was torn down at the accelerated ADR-007 Phase 4+5 cutover. QA now
# lives entirely in infra/terraform/environments/qa-app-platform/ (App
# Platform frontends + Managed PG + Odoo/obs droplets) and serves the plain
# qa.* hostnames. Frontends deploy via grove-sites CI -> GHCR ->
# deploy_on_push; droplets via terraform apply in that env.

# ── QA Level 3 (qa-app-platform) ────────────────────────────────────────────
# Lifecycle for the current QA env. Secrets flow: 1Password -> `op run
# --env-file`, which resolves the op:// refs in $(QA_L3_DIR)/.env.op and
# injects them as TF_VAR_*/AWS_* for the wrapped terraform. Requires `op`
# signed in (Goldberry Grove - Admin vault). Infisical is retired (GOL-231);
# these targets were its last local-ops consumer (GOL-418).
#
# GROVE_BRAND_PR_TOKEN is intentionally NOT in .env.op — it has no 1Password
# home yet, and an op:// ref to a missing field is a hard `op run` failure.
# It stays an optional passthrough (":-" default empty): the brand-entry
# endpoint fails safe (503) while /optimize works with the grove_assets_* vars.

QA_L3_DIR := infra/terraform/environments/qa-app-platform
QA_L3_ENV_FILE := $(QA_L3_DIR)/.env.op

# backend.hcl is gitignored (it is generated, not authored) — regenerate it on
# every run so a clean checkout works and the config cannot drift from CI's.
# Values mirror .github/workflows/terraform-drift.yml's "Write backend config".
# Credentials are NOT written here: the S3 backend picks them up from the
# AWS_* env vars `op run` injects, so no secret ever touches the filesystem.
define QA_L3_BACKEND_HCL
endpoint                    = "https://nyc3.digitaloceanspaces.com"
bucket                      = "grove-tf-state"
key                         = "qa-app-platform/terraform.tfstate"
region                      = "us-east-1"
skip_credentials_validation = true
skip_metadata_api_check     = true
skip_region_validation      = true
skip_requesting_account_id  = true
force_path_style            = true
endef
export QA_L3_BACKEND_HCL

.PHONY: qa-l3-backend
qa-l3-backend:
	@echo "$$QA_L3_BACKEND_HCL" > $(QA_L3_DIR)/backend.hcl

## qa-l3-plan: preview changes to the Level 3 QA env (read-only; run before qa-l3-up)
.PHONY: qa-l3-plan
qa-l3-plan: qa-l3-backend
	@op run --env-file=$(QA_L3_ENV_FILE) -- bash -c '\
		export TF_VAR_grove_brand_pr_token="$${GROVE_BRAND_PR_TOKEN:-}"; \
		PUBLISH_SECRET_GUARD_WARN_ONLY=1 bash scripts/check-publish-webhook-secrets-wired.sh; \
		terraform -chdir=$(QA_L3_DIR) init -backend-config=backend.hcl -input=false >/dev/null; \
		terraform -chdir=$(QA_L3_DIR) plan -input=false'

## qa-l3-up: apply the full Level 3 QA env (droplets re-bootstrap from cloud-init)
# GOL-2518: gated on the publish-webhook secret guard -- with the .env.op refs
# commented out the TF vars default to "", so an apply here silently ZEROES the
# live per-tenant HMAC secret on BOTH halves (droplet .env + DO app env) and the
# publish path dies without an error. Override: ALLOW_EMPTY_PUBLISH_SECRETS=1.
.PHONY: qa-l3-up
qa-l3-up: qa-l3-backend
	@op run --env-file=$(QA_L3_ENV_FILE) -- bash -c '\
		export TF_VAR_grove_brand_pr_token="$${GROVE_BRAND_PR_TOKEN:-}"; \
		bash scripts/check-publish-webhook-secrets-wired.sh && \
		terraform -chdir=$(QA_L3_DIR) init -backend-config=backend.hcl -input=false >/dev/null && \
		terraform -chdir=$(QA_L3_DIR) apply -input=false'

## qa-check-publish-secrets: verify an apply would not zero a publish-webhook secret
.PHONY: qa-check-publish-secrets
qa-check-publish-secrets:
	@bash scripts/check-publish-webhook-secrets-wired.sh

## qa-l3-teardown: destroy compute only (apps + droplets); PG data/DNS/certs survive
.PHONY: qa-l3-teardown
qa-l3-teardown:
	bash scripts/qa-l3-teardown.sh compute

## qa-l3-teardown-all: destroy EVERYTHING incl. Managed PG data + DNS zone
.PHONY: qa-l3-teardown-all
qa-l3-teardown-all:
	bash scripts/qa-l3-teardown.sh all

# ── Release Train cadence aliases (GOL-2326 / GOL-2324) ──────────────────────
# The biweekly Grove Release Train's two spend-bracketing legs, named to match
# the epic vocabulary. Thin aliases over the qa-l3 targets so "one command"
# lines up with "train-up" / "train-teardown". Both stay LOCAL + human-run by
# design (creds live in the Goldberry Grove - Admin / Grove QA vaults, not CI;
# teardown needs a delete-scoped token CI deliberately lacks). A scheduled
# Discord reminder (.github/workflows/release-train-reminder.yml) nudges the
# cadence; the human running it IS the approval on spend. See
# docs/RUNBOOK-release-train.md.

## train-up: (Mon) bring the biweekly QA window up — alias for qa-l3-up (idempotent apply)
.PHONY: train-up
train-up: qa-l3-up

## train-teardown: (Thu) tear the QA compute down — alias for qa-l3-teardown (typed-confirm)
.PHONY: train-teardown
train-teardown: qa-l3-teardown

# ── QA E2E test-inventory fixture seed (GOL-1152) ────────────────────────────
# Idempotently seed the Playwright E2E test-inventory fixture (a Potted-only,
# in-stock nursery product) into the QA Odoo so a rebuilt QA comes up
# E2E-test-ready with NO manual reseed. Run once after `qa-l3-up` (or any QA
# rebuild) has the Odoo droplet serving. The seed script (GOL-1148) lives in
# grove-odoo-modules; it is an XML-RPC client, so it runs from here against the
# network-reachable QA Odoo. It is fetched pinned to a ref (default main) so a
# clean odoocker checkout works without the sibling repo. Creds flow from
# 1Password via the same `op run --env-file` path the apply uses; see
# $(QA_L3_DIR)/.env.op.seed for the vault refs and the "why local-ops not CI"
# note (grove-ci-prod-ro cannot read Grove QA). Idempotent: a converged fixture
# is a no-op re-run. Pass DRY_RUN=1 for a read-only plan.
QA_L3_SEED_ENV_FILE := $(QA_L3_DIR)/.env.op.seed
SEED_E2E_REF ?= main
SEED_E2E_URL := https://raw.githubusercontent.com/Goldberry-Playground/grove-odoo-modules/$(SEED_E2E_REF)/scripts/seed_e2e_test_inventory.py

## qa-l3-seed-e2e: seed the Playwright E2E test-inventory fixture into QA Odoo (idempotent; DRY_RUN=1 for a plan)
.PHONY: qa-l3-seed-e2e
qa-l3-seed-e2e:
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
		curl -fsSL "$(SEED_E2E_URL)" -o "$$tmp"; \
		DRY_RUN="$(DRY_RUN)" op run --env-file=$(QA_L3_SEED_ENV_FILE) -- python3 "$$tmp"

## qa-test-data-cleanup: DRY-RUN report of QA test data (canary/journey orders, test partners) — deletes nothing
.PHONY: qa-test-data-cleanup
qa-test-data-cleanup:
	bash scripts/qa-test-data-cleanup.sh $(ARGS)

## qa-test-data-cleanup-apply: actually remove QA test data (surgical; reserved-domain markers only)
.PHONY: qa-test-data-cleanup-apply
qa-test-data-cleanup-apply:
	bash scripts/qa-test-data-cleanup.sh --apply $(ARGS)

# ── Monitoring / synthetics (GOL-2325) ───────────────────────────────────────
# Config-as-code bring-up for the observability stack + Tier-1 synthetic runner.
# `monitoring-up` depends on `monitoring-setup` so the runner is NEVER started
# without its seed records (canary product + monitors/alerts uploaded) — spec
# docs/specs/2026-06-26-grove-observability-design.md §1.
#
# Secrets: the deploy's --env-file (.env.monitoring) holds RESOLVED values on
# the droplet; populate it from 1Password with `op run` (see the prod go-live
# runbook, docs/RUNBOOK-prod-synthetics-golive.md). Override the file/compose on
# the CLI: make monitoring-up MONITORING_ENV_FILE=.env.monitoring.prod
MONITORING_COMPOSE   ?= docker-compose.monitoring.yml
MONITORING_ENV_FILE  ?= .env.monitoring

## monitoring-setup: seed the $0 canary + upload monitors/alerts/dashboards (idempotent)
.PHONY: monitoring-setup
monitoring-setup:
	@test -f "$(MONITORING_ENV_FILE)" || { echo "missing $(MONITORING_ENV_FILE) — populate it from 1Password first (see docs/RUNBOOK-prod-synthetics-golive.md)"; exit 1; }
	@case "$(MONITORING_ENV_FILE)" in \
		*/*) set -a; . "$(MONITORING_ENV_FILE)"; set +a; python3 scripts/setup-monitoring.py ;; \
		*) set -a; . ./"$(MONITORING_ENV_FILE)"; set +a; python3 scripts/setup-monitoring.py ;; \
	esac

## monitoring-up: bring up the monitoring stack — seeds first, so the runner never fires without seed records
.PHONY: monitoring-up
monitoring-up: monitoring-setup
	docker compose -f $(MONITORING_COMPOSE) --env-file $(MONITORING_ENV_FILE) up -d

## monitoring-down: stop the monitoring stack (keeps volumes)
.PHONY: monitoring-down
monitoring-down:
	docker compose -f $(MONITORING_COMPOSE) --env-file $(MONITORING_ENV_FILE) down

# ── Help ─────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo ""
	@echo "Grove Odoocker — Makefile targets"
	@echo ""
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
	@echo ""
