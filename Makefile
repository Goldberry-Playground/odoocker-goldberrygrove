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
		terraform -chdir=$(QA_L3_DIR) init -backend-config=backend.hcl -input=false >/dev/null; \
		terraform -chdir=$(QA_L3_DIR) plan -input=false'

## qa-l3-up: apply the full Level 3 QA env (droplets re-bootstrap from cloud-init)
.PHONY: qa-l3-up
qa-l3-up: qa-l3-backend
	@op run --env-file=$(QA_L3_ENV_FILE) -- bash -c '\
		export TF_VAR_grove_brand_pr_token="$${GROVE_BRAND_PR_TOKEN:-}"; \
		terraform -chdir=$(QA_L3_DIR) init -backend-config=backend.hcl -input=false >/dev/null; \
		terraform -chdir=$(QA_L3_DIR) apply -input=false'

## qa-l3-teardown: destroy compute only (apps + droplets); PG data/DNS/certs survive
.PHONY: qa-l3-teardown
qa-l3-teardown:
	bash scripts/qa-l3-teardown.sh compute

## qa-l3-teardown-all: destroy EVERYTHING incl. Managed PG data + DNS zone
.PHONY: qa-l3-teardown-all
qa-l3-teardown-all:
	bash scripts/qa-l3-teardown.sh all

# ── Help ─────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo ""
	@echo "Grove Odoocker — Makefile targets"
	@echo ""
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /'
	@echo ""
