#!/usr/bin/env python3
"""Regression tests for scripts/prod-modules-promote.sh (GOL-2346, Leg B).

No network, no SSH, no Docker. The script is driven against a SIMULATED prod
droplet: a stub `ssh` that runs the remote payload locally, and a stub `docker`
that models just enough of `docker compose` (the git-sync sidecar, the
entrypoint's revision marker, and the odoo log) to exercise every branch.

The properties tested are the ones that would actually cost money or an outage:

  rejects-non-sha            a branch name / short hash never reaches the
                             droplet (mirrors var.custom_modules_ref's own
                             40-hex validation, GOL-892).
  rejects-bad-confirm        only the literal CONFIRM=PROMOTE mutates prod.
  preflight-writes-nothing   the default mode reports the delta and leaves
                             /etc/grove/.env byte-identical.
  aborts-without-auto-upgrade  a droplet whose DEPLOYED compose predates
                             AUTO_UPGRADE_MODULES is refused (exit 4) instead
                             of being restarted into a half-migrated DB.
  promote-happy-path         .env upserted exactly once, git-sync resynced,
                             marker advanced, full tax bind accepted.
  money-guard-partial-bind   "bound for only X of N" => non-zero exit, even
                             though the upgrade itself "succeeded" (GOL-2449:
                             setup_wv_sales_tax swallows per-company failures
                             and still exits 0).
  money-guard-missing-line   no bind line is NOT a failure by itself -- Odoo
                             skips migrations/19.0.1.47.0 whenever the DB's
                             recorded version already covers it (QA hit this on
                             2026-09-23). The guard says which case it is and
                             then lets the database decide.
  money-guard-db-is-truth    the direct DB read runs on EVERY promote: a clean
                             "3 of 3" in the log cannot launder a mis-bound
                             database (exit 9), and a probe that returns
                             nothing leaves the binding UNVERIFIED (exit 8).
  preflight-reports-version  the pre-flight prints ir_module_module's recorded
                             version and says up front whether the WV-tax
                             migration will run or be skipped.
  sync-timeout-leaves-odoo-alone  if git-sync never reaches the target, odoo is
                             NOT restarted (prod keeps serving the old code).
  idempotent-rerun           a second promote at the same SHA is a NO-OP and
                             writes no second backup.
  no-duplicate-env-key       the upsert rewrites in place; compose reads the
                             last occurrence, so a duplicate key would be a
                             silent split-brain.
  perenual-off-by-default    with PERENUAL_API_KEY unset the promote is
                             byte-for-byte what it was: no .env line, no compose
                             edit, a plain `restart` rather than a recreate.
  perenual-validates-locally a key with whitespace/metacharacters is refused
                             before the droplet is touched -- cloud-init writes
                             it UNQUOTED into a bash-sourced /etc/grove/.env, so
                             a bad value breaks the NEXT boot, not this run.
  perenual-never-echoed      the key appears in NO output on any path, pass or
                             fail (a promote log is pasted into tickets).
  perenual-converges         .env line + compose passthrough + a RECREATE (a
                             plain restart keeps the old container env, which is
                             how an "activated" key silently stays inert).
  perenual-noop-shortcut     a droplet already ON the target SHA must NOT take
                             the NO-OP exit while the converge is still pending
                             -- that path would silently skip the activation.
  perenual-runtime-verified  if the var does not reach the odoo PROCESS after
                             the recreate, exit 10 rather than report success.
  stripe-tax-off-by-default  with every GROVE_STRIPE_TAX_* unset the promote is
                             byte-for-byte what it was: no .env line, no compose
                             edit, a plain `restart` rather than a recreate.
  stripe-tax-token-contract  only the eight tokens `_stripe_tax_enabled`
                             recognises are accepted, and ONLY locally (no SSH).
                             grove_headless treats an unrecognised value as OFF
                             WITHOUT COMPLAINING, so `ture` would otherwise
                             converge, verify and report success while prod kept
                             charging Odoo tax. This is the money-path bug the
                             validation exists for.
  stripe-tax-converges       .env line + compose passthrough + a RECREATE, for
                             one tenant or several, and the falsey token
                             (the ROLLBACK) converges and proves itself the
                             same way the truthy one does.
  stripe-tax-idempotent      a second run at the same value is a NO-OP: one
                             .env line, one compose backup, no second edit.
  stripe-tax-runtime-verified  files converged + the flag NOT in the odoo
                             process env is the state that looks live and still
                             charges Odoo tax => exit 10, never success.

    python3 scripts/test_prod_modules_promote.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "prod-modules-promote.sh")

OLD = "0b36ecfa7dfc27ad7f17f5917652a3287221f124"
NEW = "cf519bfa8ee59c493fc94eee15d77a504e7a09a7"

# --- the stub droplet -------------------------------------------------------
# State lives in plain files under $FAKE_STATE so both stubs and the assertions
# can read it. `docker` models only the four invocations the script makes.

STUB_SSH = """#!/usr/bin/env bash
# Stub ssh: ignore host/flags, run the remote payload (last arg) locally.
payload="${@: -1}"
exec bash -c "$payload"
"""

STUB_DOCKER = r"""#!/usr/bin/env bash
# Stub `docker compose ...`. Recognises exactly what prod-modules-promote.sh
# calls; anything else is a loud failure so the test notices a drifted call.
S="$FAKE_STATE"
args=("$@")
# drop: compose --env-file <path>
rest=("${args[@]:3}")
cmd="${rest[0]}"

case "$cmd" in
  exec)
    # exec -T <service> ...
    svc="${rest[2]}"
    if [ "$svc" = "custom-modules-sync" ]; then
      [ -f "$S/synced" ] || exit 1
      cat "$S/synced"; exit 0
    fi
    # odoo: symlink-basename fallback, manifest grep, or an `odoo shell` probe
    joined="${rest[*]}"
    case "$joined" in
      *printenv*)
        # Models the container env as baked at CREATE time: `restart` cannot
        # change it, only a recreate re-reads compose + .env. Keyed per var --
        # the script asks for PERENUAL_API_KEY and each GROVE_STRIPE_TAX_*
        # separately, and "absent" (exit 1) has to stay distinguishable from
        # "present but empty".
        key="${rest[${#rest[@]}-1]}"
        [ -f "$S/container_env/$key" ] || exit 1
        cat "$S/container_env/$key"
        exit 0 ;;
      *"odoo shell"*)
        # The probe script arrives on stdin; which one it is decides the reply.
        script="$(cat)"
        case "$script" in
          *GROVEVER*)
            # empty file models an unreadable / unresolvable version
            if [ -s "$S/recorded_version" ]; then
              echo "GROVEVER|grove_headless|installed|$(cat "$S/recorded_version")"
            fi
            exit 0 ;;
          *GROVETAX*)
            cat "$S/tax_out" 2>/dev/null || true
            exit 0 ;;
        esac
        echo "stub-docker: unhandled odoo shell probe" >&2; exit 96 ;;
      *readlink*) cat "$S/synced" 2>/dev/null || exit 1; exit 0 ;;
      *__manifest__.py*) echo '    "version": "19.0.1.51.0",'; exit 0 ;;
    esac
    echo "stub-docker: unhandled exec: $joined" >&2; exit 97
    ;;
  config)
    exit 0
    ;;
  up)
    svc="${rest[${#rest[@]}-1]}"
    if [ "$svc" = "odoo" ]; then
      # Recreate: same entrypoint pass as `restart`, PLUS the container env is
      # rebuilt from the deployed compose + .env (the whole reason the script
      # recreates instead of restarting).
      touch "$S/odoo_restarted"; touch "$S/odoo_recreated"
      synced="$(cat "$S/synced")"
      cat "$S/upgrade_log" >> "$S/logs" 2>/dev/null || true
      printf '%s\n' "$synced" > "$S/marker"
      compose="$(dirname "$(readlink -f "$S/envfile")")/docker-compose.yml"
      if [ ! -f "$S/env_blackhole" ]; then
        mkdir -p "$S/container_env"
        # The whole point of the compose `environment:` block: a key that is
        # NOT listed there can never reach the process no matter what .env
        # says (GOL-1772/GOL-1786). Model exactly that.
        for key in PERENUAL_API_KEY GROVE_STRIPE_TAX_GOLDBERRY \
                   GROVE_STRIPE_TAX_GGG GROVE_STRIPE_TAX_NURSERY; do
          if grep -Eq "^[[:space:]]*$key:" "$compose"; then
            sed -n "s/^$key=//p" "$S/envfile" | tail -1 | tr -d '\r' \
              > "$S/container_env/$key"
          fi
        done
      fi
      exit 0
    fi
    # up -d --force-recreate --no-deps custom-modules-sync => git-sync adopts the
    # ref compose interpolates for GITSYNC_REF=${CUSTOM_MODULES_REF}. Real
    # `docker compose` prefers a value found in its process ENVIRONMENT over the
    # same key in --env-file (GOL-2657), and this stub is a subprocess of the
    # payload shell, so honor an exported CUSTOM_MODULES_REF exactly as compose
    # would. If the payload leaked the OLD ref via `set -a; . .env` before the
    # rewrite, git-sync stays stale here and the caller's wait loop times out
    # (exit 6) -- reproducing the real first-promote failure.
    if [ -f "$S/sync_fails" ]; then exit 0; fi
    if [ -n "${CUSTOM_MODULES_REF:-}" ]; then
      printf '%s\n' "$CUSTOM_MODULES_REF" > "$S/synced"
    else
      sed -n 's/^CUSTOM_MODULES_REF=//p' "$S/envfile" | tail -1 | tr -d '\r' > "$S/synced"
    fi
    exit 0
    ;;
  restart)
    # entrypoint: revision advanced => run the upgrade, then record the marker
    touch "$S/odoo_restarted"
    synced="$(cat "$S/synced")"
    cat "$S/upgrade_log" >> "$S/logs" 2>/dev/null || true
    printf '%s\n' "$synced" > "$S/marker"
    exit 0
    ;;
  logs)
    cat "$S/logs" 2>/dev/null || true
    exit 0
    ;;
esac
echo "stub-docker: unhandled command: ${rest[*]}" >&2
exit 98
"""

COMPOSE_WITH_AUTO = """services:
  odoo:
    environment:
      APP_ENV: production
      AUTO_UPGRADE_MODULES: grove_headless,grove_support
"""

COMPOSE_WITHOUT_AUTO = """services:
  odoo:
    environment:
      APP_ENV: production
"""

# What the direct DB probe reports. The bind line in the log is only a proxy;
# these rows are the thing that actually decides what a customer is charged.
TAX_ALL_OK = """GROVETAX|company|1|Goldberry Grove|WV State Sales Tax 6%|6.0|OK
GROVETAX|ship|1|Goldberry Grove|WV State Sales Tax 6%||OK
GROVETAX|company|8|George George George Woodworking|WV State Sales Tax 6%|6.0|OK
GROVETAX|ship|8|George George George Woodworking|WV State Sales Tax 6%||OK
GROVETAX|company|9|At The Grove Nursery|WV State Sales Tax 6%|6.0|OK
GROVETAX|ship|9|At The Grove Nursery|<absent>||SKIP
GROVETAXSUM|0|3
"""

# The GOL-2449 shape: the upgrade "succeeded", one company kept the old 7%.
TAX_ONE_BAD = """GROVETAX|company|1|Goldberry Grove|WV State Sales Tax 6%|6.0|OK
GROVETAX|ship|1|Goldberry Grove|WV State Sales Tax 6%||OK
GROVETAX|company|8|George George George Woodworking|WV State Sales Tax 6%|6.0|OK
GROVETAX|ship|8|George George George Woodworking|WV State Sales Tax 6%||OK
GROVETAX|company|9|At The Grove Nursery|WV Sales Tax 7%|7.0|BAD
GROVETAX|ship|9|At The Grove Nursery|WV State Sales Tax 6%||OK
GROVETAXSUM|1|3
"""

# Pre-1.47.0: the tax migration is pending, so a bind line IS expected.
VER_BELOW_TAX_MIGRATION = "19.0.1.40.0"
# >=1.47.0: Odoo skips the migration and logs nothing. QA hit exactly this on
# 2026-09-23 and the old guard mistook it for a failure.
VER_AT_OR_ABOVE_TAX_MIGRATION = "19.0.1.51.0"

FULL_BIND = (
    "INFO odoo grove_headless: WV 6% state sales tax bound for 3 of 3 companies\n"
)
PARTIAL_BIND = (
    "WARNING odoo grove_headless: WV tax setup FAILED for company At The Grove: boom\n"
    "INFO odoo grove_headless: WV 6% state sales tax bound for 2 of 3 companies\n"
    "WARNING odoo grove_headless: WV 6% state sales tax bound for only 2 of 3 companies\n"
)


class Droplet:
    """A temp dir that looks like /etc/grove + the stub binaries on PATH."""

    def __init__(self, *, env_ref=OLD, synced=OLD, marker=OLD,
                 auto_upgrade=True, upgrade_log=FULL_BIND, sync_fails=False,
                 recorded_version=VER_BELOW_TAX_MIGRATION, tax_out=TAX_ALL_OK,
                 perenual_env=None, compose_has_perenual=False,
                 container_perenual=None, env_blackhole=False,
                 stripe_tax_env=None, compose_stripe_tax=(),
                 container_stripe_tax=None):
        self.root = tempfile.mkdtemp(prefix="fakegrove-")
        self.deploy = os.path.join(self.root, "grove")
        self.state = os.path.join(self.root, "state")
        self.bin = os.path.join(self.root, "bin")
        for d in (self.deploy, self.state, self.bin):
            os.makedirs(d)

        self.envfile = os.path.join(self.deploy, ".env")
        with open(self.envfile, "w") as fh:
            fh.write("DB_NAME=odoo\n")
            if env_ref:
                fh.write(f"CUSTOM_MODULES_REF={env_ref}\n")
            fh.write("ODOO_TAG=latest\n")
            if perenual_env is not None:
                fh.write(f"PERENUAL_API_KEY={perenual_env}\n")
            # What cloud-init renders once #805 is on the box: the keys exist,
            # empty = OFF. `None` models a droplet that predates that chain.
            for tenant, value in (stripe_tax_env or {}).items():
                fh.write(f"GROVE_STRIPE_TAX_{tenant}={value}\n")

        compose = COMPOSE_WITH_AUTO if auto_upgrade else COMPOSE_WITHOUT_AUTO
        if compose_has_perenual:
            compose += "      PERENUAL_API_KEY: ${PERENUAL_API_KEY:-}\n"
        for tenant in compose_stripe_tax:
            key = f"GROVE_STRIPE_TAX_{tenant}"
            compose += f"      {key}: ${{{key}:-}}\n"
        with open(os.path.join(self.deploy, "docker-compose.yml"), "w") as fh:
            fh.write(compose)
        self.container_env_dir = os.path.join(self.state, "container_env")
        os.makedirs(self.container_env_dir)
        if container_perenual is not None:
            self._write(os.path.join(self.container_env_dir,
                                     "PERENUAL_API_KEY"), container_perenual)
        for tenant, value in (container_stripe_tax or {}).items():
            self._write(
                os.path.join(self.container_env_dir,
                             f"GROVE_STRIPE_TAX_{tenant}"), value)
        if env_blackhole:
            self._write(os.path.join(self.state, "env_blackhole"), "1")

        self._write(os.path.join(self.state, "synced"), synced + "\n")
        self.marker_path = os.path.join(self.state, "marker")
        self._write(self.marker_path, marker + "\n")
        self._write(os.path.join(self.state, "upgrade_log"), upgrade_log)
        self._write(os.path.join(self.state, "logs"), "")
        self._write(os.path.join(self.state, "recorded_version"), recorded_version)
        self._write(os.path.join(self.state, "tax_out"), tax_out)
        if sync_fails:
            self._write(os.path.join(self.state, "sync_fails"), "1")
        # the stub docker needs to read the same .env the script edits
        os.symlink(self.envfile, os.path.join(self.state, "envfile"))

        for name, body in (("ssh", STUB_SSH), ("docker", STUB_DOCKER)):
            path = os.path.join(self.bin, name)
            self._write(path, body)
            os.chmod(path, 0o755)

    @staticmethod
    def _write(path, body):
        with open(path, "w") as fh:
            fh.write(body)

    def run(self, *, target=NEW, confirm=None, extra_env=None):
        env = dict(os.environ)
        env.update(
            PATH=self.bin + os.pathsep + env["PATH"],
            FAKE_STATE=self.state,
            PROD_HOST="root@fake-prod",
            DEPLOY_DIR=self.deploy,
            MARKER=self.marker_path,
            TARGET_REF=target,
            POLL_INTERVAL="1",
            SYNC_TIMEOUT="2",
            UPGRADE_TIMEOUT="2",
        )
        if confirm is not None:
            env["CONFIRM"] = confirm
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            ["bash", SCRIPT], env=env, capture_output=True, text=True
        )

    def env_text(self):
        with open(self.envfile) as fh:
            return fh.read()

    def backups(self):
        return sorted(
            f for f in os.listdir(self.deploy) if f.startswith(".env.bak.")
        )

    def odoo_restarted(self):
        return os.path.exists(os.path.join(self.state, "odoo_restarted"))

    def odoo_recreated(self):
        return os.path.exists(os.path.join(self.state, "odoo_recreated"))

    def compose_text(self):
        with open(os.path.join(self.deploy, "docker-compose.yml")) as fh:
            return fh.read()

    def compose_backups(self):
        return sorted(
            f for f in os.listdir(self.deploy)
            if f.startswith("docker-compose.yml.bak.")
        )

    def container_env(self, key):
        """What the running container's process env holds, or None if the key
        never made it in at all (the two states the script must tell apart)."""
        path = os.path.join(self.container_env_dir, key)
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return fh.read().strip()

    def container_perenual(self):
        return self.container_env("PERENUAL_API_KEY")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_rejects_non_sha():
    d = Droplet()
    try:
        for bad in ("main", "cf519bf", "CF519BFA8EE59C493FC94EEE15D77A504E7A09A7"):
            r = d.run(target=bad, confirm="PROMOTE")
            check(
                f"rejects-non-sha[{bad}]",
                r.returncode == 2 and "40-char lowercase hex" in r.stderr,
                f"rc={r.returncode} err={r.stderr[:120]}",
            )
            check(
                f"rejects-non-sha[{bad}]-no-write",
                f"CUSTOM_MODULES_REF={OLD}" in d.env_text(),
            )
    finally:
        d.cleanup()


def test_rejects_cloudflare_proxied_host():
    # The proxied hostname resolves to Cloudflare edge IPs that never carry
    # port 22; the script must refuse it up front, before any ssh, instead of
    # hanging on the connect (2026-09-30).
    d = Droplet()
    try:
        for host in ("root@odoo.gatheringatthegrove.com", "odoo.gatheringatthegrove.com"):
            r = d.run(confirm="PROMOTE", extra_env={"PROD_HOST": host})
            check(
                f"rejects-proxied-host[{host}]",
                r.returncode == 2 and "Cloudflare-proxied" in r.stderr,
                f"rc={r.returncode} err={r.stderr[:120]}",
            )
            check(
                f"rejects-proxied-host[{host}]-no-write",
                f"CUSTOM_MODULES_REF={OLD}" in d.env_text(),
            )
    finally:
        d.cleanup()


def test_rejects_bad_confirm():
    d = Droplet()
    try:
        r = d.run(confirm="promote")
        check(
            "rejects-bad-confirm",
            r.returncode == 2 and "CONFIRM must be exactly PROMOTE" in r.stderr,
            f"rc={r.returncode}",
        )
        check("rejects-bad-confirm-no-write", f"CUSTOM_MODULES_REF={OLD}" in d.env_text())
    finally:
        d.cleanup()


def test_preflight_writes_nothing():
    d = Droplet()
    try:
        before = d.env_text()
        r = d.run()
        check("preflight-exit-0", r.returncode == 0, f"rc={r.returncode} {r.stderr[:200]}")
        check("preflight-reports-delta", f"{OLD} -> {NEW}" in r.stdout, r.stdout[-300:])
        check("preflight-writes-nothing", d.env_text() == before)
        check("preflight-no-backup", d.backups() == [])
        check("preflight-no-restart", not d.odoo_restarted())
    finally:
        d.cleanup()


def test_preflight_reports_recorded_version():
    d = Droplet(recorded_version=VER_BELOW_TAX_MIGRATION)
    try:
        r = d.run()
        check(
            "preflight-prints-recorded-version",
            f"installed_version = {VER_BELOW_TAX_MIGRATION}" in r.stdout,
            r.stdout[-500:],
        )
        check(
            "preflight-says-tax-migration-will-run",
            "WV-tax migration WILL run" in r.stdout,
            r.stdout[-500:],
        )
    finally:
        d.cleanup()

    d = Droplet(recorded_version=VER_AT_OR_ABOVE_TAX_MIGRATION)
    try:
        r = d.run()
        check(
            "preflight-says-tax-migration-will-be-skipped",
            "will SKIP the WV-tax migration" in r.stdout,
            r.stdout[-500:],
        )
    finally:
        d.cleanup()

    d = Droplet(recorded_version="")
    try:
        r = d.run()
        check(
            "preflight-tolerates-unreadable-version",
            r.returncode == 0 and "recorded version unreadable" in r.stdout,
            f"rc={r.returncode} {r.stdout[-500:]}",
        )
    finally:
        d.cleanup()


def test_preflight_noop_when_already_current():
    d = Droplet(env_ref=NEW, synced=NEW, marker=NEW)
    try:
        r = d.run()
        check("noop-exit-0", r.returncode == 0, f"rc={r.returncode}")
        check("noop-reported", "NO-OP" in r.stdout, r.stdout[-300:])
    finally:
        d.cleanup()


def test_aborts_without_auto_upgrade():
    d = Droplet(auto_upgrade=False)
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "aborts-without-auto-upgrade",
            r.returncode == 4 and "AUTO_UPGRADE_MODULES" in r.stderr,
            f"rc={r.returncode} err={r.stderr[:200]}",
        )
        check("aborts-without-auto-upgrade-no-write", f"CUSTOM_MODULES_REF={OLD}" in d.env_text())
        check("aborts-without-auto-upgrade-no-restart", not d.odoo_restarted())
    finally:
        d.cleanup()


def test_promote_happy_path():
    d = Droplet()
    try:
        r = d.run(confirm="PROMOTE")
        check("promote-exit-0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[-400:]}")
        check("promote-env-upserted", f"CUSTOM_MODULES_REF={NEW}" in d.env_text())
        check(
            "no-duplicate-env-key",
            d.env_text().count("CUSTOM_MODULES_REF=") == 1,
            d.env_text(),
        )
        check("promote-backup-kept", len(d.backups()) == 1, str(d.backups()))
        check("promote-restarted-odoo", d.odoo_restarted())
        check("promote-marker-advanced", open(d.marker_path).read().strip() == NEW)
        check("money-guard-full-bind-ok", "3 of 3 companies (full coverage)" in r.stdout, r.stdout[-400:])
        check("promote-prints-reconcile-next-step", "reconcile-modules-pin.yml" in r.stdout)
        check("promote-prints-rollback", "Rollback (if needed)" in r.stdout)
    finally:
        d.cleanup()


def test_money_guard_partial_bind():
    d = Droplet(upgrade_log=PARTIAL_BIND)
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "money-guard-partial-bind",
            r.returncode == 9 and "PARTIAL TAX BIND" in r.stderr,
            f"rc={r.returncode} err={r.stderr[-400:]}",
        )
        check(
            "money-guard-partial-names-company",
            "WV tax setup FAILED for company At The Grove" in r.stderr,
            r.stderr[-400:],
        )
    finally:
        d.cleanup()


NO_BIND_LINE = "INFO odoo Modules loaded.\n"


def test_money_guard_missing_line_but_db_verified():
    """The QA 2026-09-23 case: recorded version >= the migration, so Odoo skips
    it and logs no bind line. The old guard exited 8 on a perfectly bound DB."""
    d = Droplet(
        upgrade_log=NO_BIND_LINE,
        recorded_version=VER_AT_OR_ABOVE_TAX_MIGRATION,
    )
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "missing-line-skipped-migration-is-not-a-failure",
            r.returncode == 0,
            f"rc={r.returncode} err={r.stderr[-500:]}",
        )
        check(
            "missing-line-explains-why",
            "no bind line -- EXPECTED" in r.stdout,
            r.stdout[-500:],
        )
        check(
            "missing-line-falls-back-to-db-read",
            "all 3 companies verified on" in r.stdout,
            r.stdout[-500:],
        )
    finally:
        d.cleanup()


def test_money_guard_missing_line_and_db_bad():
    """No bind line AND the DB disagrees => exit 9, never a silent pass."""
    d = Droplet(
        upgrade_log=NO_BIND_LINE,
        recorded_version=VER_AT_OR_ABOVE_TAX_MIGRATION,
        tax_out=TAX_ONE_BAD,
    )
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "missing-line-bad-db-exits-9",
            r.returncode == 9 and "VERIFIED WRONG TAX BINDING" in r.stderr,
            f"rc={r.returncode} err={r.stderr[-500:]}",
        )
        check(
            "missing-line-bad-db-names-the-row",
            "At The Grove Nursery|WV Sales Tax 7%" in r.stdout,
            r.stdout[-600:],
        )
    finally:
        d.cleanup()


def test_money_guard_missing_line_when_migration_was_due():
    """No bind line when the pre-flight said one was DUE is suspicious even if
    the DB happens to check out -- say so loudly, but let the DB decide."""
    d = Droplet(upgrade_log=NO_BIND_LINE, recorded_version=VER_BELOW_TAX_MIGRATION)
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "missing-line-when-due-warns",
            r.returncode == 0 and "no bind line in the log window" in r.stderr,
            f"rc={r.returncode} err={r.stderr[-500:]}",
        )
    finally:
        d.cleanup()


def test_money_guard_db_overrides_a_clean_log_line():
    """The strongest property: a full "3 of 3" in the log can NOT launder a
    database that is actually mis-bound."""
    d = Droplet(upgrade_log=FULL_BIND, tax_out=TAX_ONE_BAD)
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "clean-log-line-cannot-launder-bad-db",
            r.returncode == 9 and "VERIFIED WRONG TAX BINDING" in r.stderr,
            f"rc={r.returncode} err={r.stderr[-500:]}",
        )
    finally:
        d.cleanup()


def test_money_guard_unverifiable_db():
    """The probe returned nothing: the binding is UNKNOWN, which is not OK."""
    d = Droplet(tax_out="")
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "unverifiable-db-exits-8",
            r.returncode == 8 and "could not read the tax binding" in r.stderr,
            f"rc={r.returncode} err={r.stderr[-500:]}",
        )
    finally:
        d.cleanup()


def test_sync_timeout_leaves_odoo_alone():
    d = Droplet(sync_fails=True)
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "sync-timeout-exit-6",
            r.returncode == 6 and "git-sync is on" in r.stderr,
            f"rc={r.returncode} err={r.stderr[-400:]}",
        )
        check("sync-timeout-no-restart", not d.odoo_restarted())
        check("sync-timeout-offers-rollback", "Roll back the env file" in r.stderr)
    finally:
        d.cleanup()


def test_promote_does_not_leak_stale_ref_to_gitsync():
    """GOL-2657: the payload must not `set -a; . .env` the whole deploy env
    before rewriting it. Sourcing exports the OLD CUSTOM_MODULES_REF, and docker
    compose interpolation prefers the shell environment over --env-file, so the
    `dc up --force-recreate custom-modules-sync` recreates git-sync on the stale
    ref -- the wait loop then times out (exit 6) on the first promote. The stub
    docker honors an exported ref exactly as real compose does, so a leak makes
    git-sync stay on OLD and this test fails."""
    d = Droplet(env_ref=OLD, synced=OLD, marker=OLD)
    try:
        r = d.run(confirm="PROMOTE")
        synced = open(os.path.join(d.state, "synced")).read().strip()
        check("no-stale-ref-leak-exit-0", r.returncode == 0,
              f"rc={r.returncode} err={r.stderr[-400:]}")
        check("no-stale-ref-leak-gitsync-on-target", synced == NEW,
              f"git-sync landed on {synced!r}, expected {NEW!r} -- stale ref leaked")
        check("no-stale-ref-leak-marker-advanced",
              open(d.marker_path).read().strip() == NEW)
    finally:
        d.cleanup()


# An ssh stub that captures the rendered remote payload instead of running it,
# so a test can assert on the exact text that would reach the droplet.
CAPTURE_SSH = '#!/usr/bin/env bash\nprintf "%s" "${@: -1}" > "$FAKE_STATE/payload"\n'


def test_payload_does_not_bulk_source_env():
    """GOL-2657 (static): render the remote payload and assert it never bulk
    exports the deploy env (`set -a; . .env`). That bulk source is what leaks
    every var the script later rewrites (CUSTOM_MODULES_REF, PERENUAL_API_KEY)
    into the shell, where compose interpolation prefers it over --env-file. Only
    the one var actually needed (DB_NAME) may be read, via a targeted sed."""
    d = Droplet()
    try:
        Droplet._write(os.path.join(d.bin, "ssh"), CAPTURE_SSH)
        os.chmod(os.path.join(d.bin, "ssh"), 0o755)
        d.run(confirm="PROMOTE")
        payload = open(os.path.join(d.state, "payload")).read()
        # Assert on executable statements only -- the fix's own comment explains
        # the `set -a` trap in prose, which is not itself a bulk source.
        active = "\n".join(
            ln for ln in payload.splitlines() if not ln.lstrip().startswith("#")
        )
        check("payload-no-bulk-source", "set -a" not in active,
              "payload still bulk-sources .env (set -a) before the rewrite")
        check("payload-extracts-db-name-targeted", "s/^DB_NAME=" in active,
              "DB_NAME is no longer read via a targeted sed")
    finally:
        d.cleanup()


def test_idempotent_rerun():
    d = Droplet()
    try:
        first = d.run(confirm="PROMOTE")
        check("idempotent-first-ok", first.returncode == 0, f"rc={first.returncode}")
        second = d.run(confirm="PROMOTE")
        check("idempotent-second-noop", second.returncode == 0 and "NO-OP" in second.stdout,
              f"rc={second.returncode} {second.stdout[-300:]}")
        check("idempotent-no-second-backup", len(d.backups()) == 1, str(d.backups()))
        check("idempotent-env-single-key", d.env_text().count("CUSTOM_MODULES_REF=") == 1)
    finally:
        d.cleanup()


def test_appends_key_when_absent():
    d = Droplet(env_ref=None)
    try:
        r = d.run(confirm="PROMOTE")
        check("appends-key-when-absent", r.returncode == 0 and
              d.env_text().count(f"CUSTOM_MODULES_REF={NEW}") == 1,
              f"rc={r.returncode} env={d.env_text()!r}")
    finally:
        d.cleanup()


# --- PERENUAL_API_KEY converge (GOL-2507) ----------------------------------
# The Perenual half of a prod promote: the key must land in /etc/grove/.env AND
# in the odoo service's compose `environment:` AND in the running container's
# process env -- miss any one and the enrich cron silently no-ops with every
# job left queued, which looks exactly like success.

# Deliberately shaped like a placeholder, not like a key: `your-key-here` is a
# .gitleaks.toml allowlist regex, and a realistic-looking fixture would (and did)
# trip `generic-api-key` on the full-history scan. Still has to satisfy the
# script's own charset validation, so no underscores.
FAKE_KEY = "your-key-here-gol2507-fixture"
PASSTHROUGH = "PERENUAL_API_KEY: ${PERENUAL_API_KEY:-}"


def _no_leak(name, r, secret=FAKE_KEY):
    """A promote log gets pasted into tickets and Discord. The key must not be
    in it -- on ANY path, including the failure ones."""
    check(f"{name}-never-echoed", secret not in r.stdout and secret not in r.stderr)


def test_perenual_off_by_default():
    """Unset => the promote is exactly what it was: no .env line, no compose
    edit, and a plain `restart` (not a recreate)."""
    d = Droplet()
    try:
        r = d.run(confirm="PROMOTE")
        check("perenual-off-exit-0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-300:]}")
        check("perenual-off-no-env-line", "PERENUAL_API_KEY=" not in d.env_text())
        check("perenual-off-no-compose-edit", PASSTHROUGH not in d.compose_text())
        check("perenual-off-no-compose-backup", d.compose_backups() == [])
        check("perenual-off-restart-not-recreate",
              d.odoo_restarted() and not d.odoo_recreated())
        # The state line is still reported, so a pre-flight always answers
        # "is Perenual live on prod?" without being asked to converge.
        check("perenual-off-still-reports-state", "PERENUAL_API_KEY (GOL-2507)" in r.stdout)
    finally:
        d.cleanup()


def test_perenual_validates_locally():
    """A value that would break the NEXT boot is refused before the droplet is
    touched -- /etc/grove/.env is bash-sourced under `set -euo pipefail` and
    cloud-init writes the value UNQUOTED."""
    d = Droplet()
    try:
        for bad in ("has space", "semi;colon", "$(whoami)", "short"):
            r = d.run(confirm="PROMOTE", extra_env={"PERENUAL_API_KEY": bad})
            check(
                f"perenual-rejects[{bad}]",
                r.returncode == 2 and "PERENUAL_API_KEY must be" in r.stderr,
                f"rc={r.returncode} err={r.stderr[:160]}",
            )
            check(f"perenual-rejects[{bad}]-no-write",
                  "PERENUAL_API_KEY=" not in d.env_text() and not d.odoo_restarted())
            _no_leak(f"perenual-rejects[{bad}]", r, bad)
    finally:
        d.cleanup()


def test_perenual_preflight_writes_nothing():
    d = Droplet()
    try:
        before_env, before_compose = d.env_text(), d.compose_text()
        r = d.run(extra_env={"PERENUAL_API_KEY": FAKE_KEY})
        check("perenual-preflight-exit-0", r.returncode == 0, f"rc={r.returncode}")
        check("perenual-preflight-says-pending", "converge PENDING" in r.stdout, r.stdout[-400:])
        check("perenual-preflight-writes-nothing",
              d.env_text() == before_env and d.compose_text() == before_compose)
        check("perenual-preflight-no-restart", not d.odoo_restarted())
        _no_leak("perenual-preflight", r)
    finally:
        d.cleanup()


def test_perenual_converges():
    """The happy path: all three points, and a RECREATE rather than a restart."""
    d = Droplet(container_perenual=None)
    try:
        r = d.run(confirm="PROMOTE", extra_env={"PERENUAL_API_KEY": FAKE_KEY})
        check("perenual-converge-exit-0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-400:]}")
        check("perenual-converge-env-line",
              d.env_text().count(f"PERENUAL_API_KEY={FAKE_KEY}\n") == 1, repr(d.env_text()))
        check("perenual-converge-single-key",
              d.env_text().count("PERENUAL_API_KEY=") == 1)
        check("perenual-converge-compose-passthrough", PASSTHROUGH in d.compose_text(),
              repr(d.compose_text()))
        # Indented to match its sibling, i.e. inside the odoo service's
        # environment block rather than dropped at column 0.
        check("perenual-converge-compose-indent",
              "      " + PASSTHROUGH in d.compose_text())
        check("perenual-converge-secret-not-in-compose", FAKE_KEY not in d.compose_text())
        check("perenual-converge-compose-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
        check("perenual-converge-recreated", d.odoo_recreated())
        check("perenual-converge-runtime", d.container_perenual() == FAKE_KEY,
              repr(d.container_perenual()))
        check("perenual-converge-verified", "odoo process env carries the supplied key" in r.stdout)
        _no_leak("perenual-converge", r)
    finally:
        d.cleanup()


def test_perenual_collapses_empty_line():
    """cloud-init renders `PERENUAL_API_KEY=` (empty) before the key is vaulted.
    The converge must replace it, not append a second line -- compose reads the
    LAST occurrence, so a duplicate is a silent split-brain."""
    d = Droplet(perenual_env="", compose_has_perenual=True, container_perenual="")
    try:
        r = d.run(confirm="PROMOTE", extra_env={"PERENUAL_API_KEY": FAKE_KEY})
        check("perenual-empty-line-exit-0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-400:]}")
        check("perenual-empty-line-collapsed",
              d.env_text().count("PERENUAL_API_KEY=") == 1
              and f"PERENUAL_API_KEY={FAKE_KEY}" in d.env_text(), repr(d.env_text()))
        # The passthrough was already there: no edit, so no compose backup.
        check("perenual-empty-line-no-compose-backup", d.compose_backups() == [])
        check("perenual-empty-line-recreated", d.odoo_recreated())
        _no_leak("perenual-empty-line", r)
    finally:
        d.cleanup()


def test_perenual_noop_shortcut_does_not_skip_converge():
    """THE regression this pairs with: a droplet already ON the target SHA used
    to exit 0 at the NO-OP shortcut. If the converge is still pending that exit
    would silently skip the whole activation."""
    d = Droplet(env_ref=NEW, synced=NEW, marker=NEW)
    try:
        r = d.run(target=NEW, confirm="PROMOTE", extra_env={"PERENUAL_API_KEY": FAKE_KEY})
        check("perenual-noop-not-taken", "NO-OP" not in r.stdout, r.stdout[-400:])
        check("perenual-noop-exit-0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-400:]}")
        check("perenual-noop-converged", f"PERENUAL_API_KEY={FAKE_KEY}" in d.env_text()
              and PASSTHROUGH in d.compose_text())
        _no_leak("perenual-noop", r)

        # ...and once it IS converged, the shortcut comes back.
        again = d.run(target=NEW, confirm="PROMOTE", extra_env={"PERENUAL_API_KEY": FAKE_KEY})
        check("perenual-noop-idempotent", again.returncode == 0 and "NO-OP" in again.stdout,
              f"rc={again.returncode} {again.stdout[-300:]}")
        check("perenual-noop-single-key", d.env_text().count("PERENUAL_API_KEY=") == 1)
        check("perenual-noop-one-compose-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
    finally:
        d.cleanup()


def test_perenual_runtime_unverified_fails():
    """Files converged but the var never reached the PROCESS: that is the exact
    state that looks activated and enriches nothing. Fail, do not report success."""
    d = Droplet(env_blackhole=True)
    try:
        r = d.run(confirm="PROMOTE", extra_env={"PERENUAL_API_KEY": FAKE_KEY})
        check("perenual-runtime-exit-10",
              r.returncode == 10 and "no PERENUAL_API_KEY in its process env" in r.stderr,
              f"rc={r.returncode} err={r.stderr[-400:]}")
        # The module promote itself is NOT rolled back -- the migration ran.
        check("perenual-runtime-says-modules-ok", "module promote itself SUCCEEDED" in r.stderr)
        _no_leak("perenual-runtime", r)
    finally:
        d.cleanup()


def test_perenual_wrong_runtime_value_fails():
    """Something else is interpolating the var (stale container, second env
    file). Neither value is echoed."""
    d = Droplet(env_blackhole=True, container_perenual="your-key-here-a-different-one")
    try:
        r = d.run(confirm="PROMOTE", extra_env={"PERENUAL_API_KEY": FAKE_KEY})
        check("perenual-mismatch-exit-10",
              r.returncode == 10 and "DIFFERENT PERENUAL_API_KEY" in r.stderr,
              f"rc={r.returncode} err={r.stderr[-400:]}")
        _no_leak("perenual-mismatch", r)
        check("perenual-mismatch-other-not-echoed",
              "your-key-here-a-different-one" not in r.stdout + r.stderr)
    finally:
        d.cleanup()


# --- GROVE_STRIPE_TAX_{TENANT} converge (GOL-2568 / GOL-2822) --------------
# Same three points as Perenual, but a MONEY path: the flag decides whether
# Stripe or Odoo computes what a customer is charged. The dangerous failure is
# not a crash, it is a converge that reports success while prod keeps charging
# Odoo tax -- so every test below is about proving the flag reached the
# PROCESS, or refusing before it could pretend it had.

NURSERY_KEY = "GROVE_STRIPE_TAX_NURSERY"
NURSERY_PASS = "GROVE_STRIPE_TAX_NURSERY: ${GROVE_STRIPE_TAX_NURSERY:-}"
GGG_KEY = "GROVE_STRIPE_TAX_GGG"

# The eight tokens grove_headless actually recognises
# (controllers/main.py::_stripe_tax_enabled does
# `(os.environ.get(k) or "").strip().lower() in ("1","true","yes","on")`).
TRUTHY = ("1", "true", "yes", "on")
FALSEY = ("0", "false", "no", "off")


def _env_diff(before, after):
    """Lines that differ, ignoring the CUSTOM_MODULES_REF bump every promote
    makes. Used to assert 'byte-for-byte what it is today'."""
    keep = lambda t: [  # noqa: E731
        l for l in t.splitlines() if not l.startswith("CUSTOM_MODULES_REF=")
    ]
    return [l for l in keep(after) if l not in keep(before)] + [
        l for l in keep(before) if l not in keep(after)
    ]


def test_stripe_tax_off_by_default():
    """Every GROVE_STRIPE_TAX_* unset => the promote is byte-for-byte the one
    that shipped before GOL-2822: no .env line, no compose edit, and a plain
    `restart` rather than a recreate."""
    d = Droplet()
    try:
        before_env, before_compose = d.env_text(), d.compose_text()
        r = d.run(confirm="PROMOTE")
        check("stripe-off-exit-0", r.returncode == 0, f"rc={r.returncode} {r.stderr[-300:]}")
        check("stripe-off-no-env-line", "GROVE_STRIPE_TAX" not in d.env_text(),
              repr(d.env_text()))
        check("stripe-off-env-byte-for-byte",
              _env_diff(before_env, d.env_text()) == [],
              str(_env_diff(before_env, d.env_text())))
        check("stripe-off-compose-untouched", d.compose_text() == before_compose)
        check("stripe-off-no-compose-backup", d.compose_backups() == [])
        check("stripe-off-restart-not-recreate",
              d.odoo_restarted() and not d.odoo_recreated())
        # The state line is still reported for ALL THREE tenants, so a
        # read-only pre-flight always answers "who is on Stripe Tax?".
        for tenant in ("GOLDBERRY", "GGG", "NURSERY"):
            check(f"stripe-off-still-reports-{tenant}",
                  f"GROVE_STRIPE_TAX_{tenant} (GOL-2568)" in r.stdout,
                  r.stdout[-600:])
    finally:
        d.cleanup()


def test_stripe_tax_rejects_unrecognised_token():
    """THE money-path guard. grove_headless treats anything outside its eight
    tokens as OFF *without complaining*, so `ture` would converge, verify
    against itself, print success -- and leave prod charging Odoo tax while the
    operator announced the cutover. Refuse it here, before any SSH."""
    d = Droplet()
    try:
        for bad in ("ture", "TRUE1", "enabled", "2", "y es", "$(whoami)", "1;rm"):
            r = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: bad})
            check(f"stripe-rejects[{bad}]",
                  r.returncode == 2 and "is not a recognised value" in r.stderr,
                  f"rc={r.returncode} err={r.stderr[:200]}")
            check(f"stripe-rejects[{bad}]-no-write",
                  "GROVE_STRIPE_TAX" not in d.env_text() and not d.odoo_restarted(),
                  repr(d.env_text()))
            # The refusal has to name the consequence, not just the syntax.
            check(f"stripe-rejects[{bad}]-explains",
                  "charging Odoo tax" in r.stderr, r.stderr[:300])
    finally:
        d.cleanup()


def test_stripe_tax_rejects_unknown_tenant():
    """A tenant slug typo would silently skip the tenant the operator meant to
    flip -- the same failure var.grove_stripe_tax_tenants' validation catches."""
    d = Droplet()
    try:
        r = d.run(confirm="PROMOTE",
                  extra_env={"STRIPE_TAX_TENANTS": "GOLDBERRY NURSEREY"})
        check("stripe-unknown-tenant-exit-2",
              r.returncode == 2 and "unknown tenant slug" in r.stderr,
              f"rc={r.returncode} err={r.stderr[:200]}")
        check("stripe-unknown-tenant-no-restart", not d.odoo_restarted())
    finally:
        d.cleanup()


def test_stripe_tax_preflight_writes_nothing():
    d = Droplet()
    try:
        before_env, before_compose = d.env_text(), d.compose_text()
        r = d.run(extra_env={NURSERY_KEY: "1"})
        check("stripe-preflight-exit-0", r.returncode == 0, f"rc={r.returncode}")
        check("stripe-preflight-says-pending",
              "converge PENDING for NURSERY -> '1'" in r.stdout, r.stdout[-600:])
        check("stripe-preflight-warns-money",
              "MONEY PATH" in r.stdout, r.stdout[-600:])
        check("stripe-preflight-writes-nothing",
              d.env_text() == before_env and d.compose_text() == before_compose)
        check("stripe-preflight-no-restart", not d.odoo_restarted())
    finally:
        d.cleanup()


def test_stripe_tax_converges():
    """Happy path on a droplet that predates the #805 compose chain: .env line,
    compose passthrough added, RECREATE (not restart), process env verified."""
    d = Droplet()
    try:
        r = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-converge-exit-0", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-500:]}")
        check("stripe-converge-env-line",
              d.env_text().count(f"{NURSERY_KEY}=1\n") == 1, repr(d.env_text()))
        check("stripe-converge-single-key",
              d.env_text().count(f"{NURSERY_KEY}=") == 1)
        check("stripe-converge-compose-passthrough",
              "      " + NURSERY_PASS in d.compose_text(), repr(d.compose_text()))
        check("stripe-converge-compose-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
        check("stripe-converge-recreated", d.odoo_recreated())
        check("stripe-converge-runtime", d.container_env(NURSERY_KEY) == "1",
              repr(d.container_env(NURSERY_KEY)))
        check("stripe-converge-verified",
              "hands sales tax to STRIPE TAX" in r.stdout, r.stdout[-800:])
        # The other two tenants must not be touched by a nursery-only flip.
        check("stripe-converge-no-collateral",
              "GROVE_STRIPE_TAX_GGG=" not in d.env_text()
              and "GROVE_STRIPE_TAX_GOLDBERRY=" not in d.env_text(),
              repr(d.env_text()))
    finally:
        d.cleanup()


def test_stripe_tax_accepts_every_token_and_normalises_case():
    """All eight tokens converge, and case is normalised the way
    `_stripe_tax_enabled` normalises it -- so `ON` and `on` are the same state
    and a re-run with either is a NO-OP rather than a second write.

    Every token gets a REAL converge: the accepted-token list IS the money-path
    contract, so it is worth the runs. Case normalisation is a single `tr`, so
    it is proven on one truthy and one falsey rather than all eight."""
    cases = [(t, t) for t in TRUTHY + FALSEY] + [("on", "ON"), ("off", "OFF")]
    for token, supplied in cases:
        d = Droplet(compose_stripe_tax=("NURSERY",))
        try:
            r = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: supplied})
            check(f"stripe-token[{supplied}]-exit-0", r.returncode == 0,
                  f"rc={r.returncode} {r.stderr[-300:]}")
            check(f"stripe-token[{supplied}]-normalised",
                  d.env_text().count(f"{NURSERY_KEY}={token}\n") == 1,
                  repr(d.env_text()))
            check(f"stripe-token[{supplied}]-runtime",
                  d.container_env(NURSERY_KEY) == token,
                  repr(d.container_env(NURSERY_KEY)))
            expect = ("hands sales tax to STRIPE TAX" if token in TRUTHY
                      else "keeps Odoo's computed WV tax line")
            check(f"stripe-token[{supplied}]-reports-effect",
                  expect in r.stdout, r.stdout[-400:])
        finally:
            d.cleanup()


def test_stripe_tax_rollback_is_the_same_command():
    """OFF is the rollback, and it must be PROVEN off rather than assumed: the
    falsey token converges all three points and verifies the process env, so a
    rollback that did not take is an error, not a silent no-op."""
    d = Droplet(stripe_tax_env={"NURSERY": "1"},
                compose_stripe_tax=("NURSERY",),
                container_stripe_tax={"NURSERY": "1"})
    try:
        r = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: "0"})
        check("stripe-rollback-exit-0", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-400:]}")
        check("stripe-rollback-env-line",
              d.env_text().count(f"{NURSERY_KEY}=0\n") == 1, repr(d.env_text()))
        check("stripe-rollback-single-key",
              d.env_text().count(f"{NURSERY_KEY}=") == 1)
        check("stripe-rollback-runtime", d.container_env(NURSERY_KEY) == "0",
              repr(d.container_env(NURSERY_KEY)))
        check("stripe-rollback-reports-odoo-tax",
              "keeps Odoo's computed WV tax line" in r.stdout, r.stdout[-400:])
        # Passthrough was already there => no compose edit, so no backup.
        check("stripe-rollback-no-compose-backup", d.compose_backups() == [])
    finally:
        d.cleanup()


def test_stripe_tax_collapses_cloud_init_empty_line():
    """#805's cloud-init renders `GROVE_STRIPE_TAX_NURSERY=` (empty) for an
    un-flipped tenant. The converge must REPLACE it, not append a second line:
    compose reads the LAST occurrence, so a duplicate is a split-brain between
    what the file appears to say and what the checkout charges."""
    d = Droplet(stripe_tax_env={"NURSERY": "", "GGG": "", "GOLDBERRY": ""},
                compose_stripe_tax=("GOLDBERRY", "GGG", "NURSERY"),
                container_stripe_tax={"NURSERY": "", "GGG": "", "GOLDBERRY": ""})
    try:
        r = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-empty-line-exit-0", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-400:]}")
        check("stripe-empty-line-collapsed",
              d.env_text().count(f"{NURSERY_KEY}=") == 1
              and f"{NURSERY_KEY}=1" in d.env_text(), repr(d.env_text()))
        check("stripe-empty-line-siblings-untouched",
              d.env_text().count("GROVE_STRIPE_TAX_GGG=\n") == 1
              and d.env_text().count("GROVE_STRIPE_TAX_GOLDBERRY=\n") == 1,
              repr(d.env_text()))
        check("stripe-empty-line-no-compose-backup", d.compose_backups() == [])
        check("stripe-empty-line-recreated", d.odoo_recreated())
    finally:
        d.cleanup()


def test_stripe_tax_multi_tenant_in_one_touch():
    """Two tenants, opposite directions, one converge -- and exactly ONE
    compose backup for the run, not one per key."""
    d = Droplet(stripe_tax_env={"GGG": "1"})
    try:
        r = d.run(confirm="PROMOTE",
                  extra_env={NURSERY_KEY: "1", GGG_KEY: "off"})
        check("stripe-multi-exit-0", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-500:]}")
        check("stripe-multi-nursery-on", d.container_env(NURSERY_KEY) == "1",
              repr(d.container_env(NURSERY_KEY)))
        check("stripe-multi-ggg-off", d.container_env(GGG_KEY) == "off",
              repr(d.container_env(GGG_KEY)))
        check("stripe-multi-one-compose-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
        check("stripe-multi-goldberry-untouched",
              "GROVE_STRIPE_TAX_GOLDBERRY=" not in d.env_text(),
              repr(d.env_text()))
    finally:
        d.cleanup()


def test_stripe_tax_idempotent_rerun():
    """A second run at the same value is a NO-OP: no second .env line, no
    second compose backup, no second recreate."""
    d = Droplet()
    try:
        first = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-idem-first-ok", first.returncode == 0,
              f"rc={first.returncode} {first.stderr[-400:]}")
        second = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-idem-second-noop",
              second.returncode == 0 and "NO-OP" in second.stdout,
              f"rc={second.returncode} {second.stdout[-400:]}")
        check("stripe-idem-single-key", d.env_text().count(f"{NURSERY_KEY}=") == 1,
              repr(d.env_text()))
        check("stripe-idem-one-compose-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
        check("stripe-idem-one-env-backup", len(d.backups()) == 1, str(d.backups()))
    finally:
        d.cleanup()


def test_stripe_tax_noop_shortcut_does_not_skip_converge():
    """A droplet already ON the target SHA must not take the NO-OP exit while a
    flag converge is still pending -- that exit would silently skip the flip
    the run was asked to do, and report success."""
    d = Droplet(env_ref=NEW, synced=NEW, marker=NEW)
    try:
        r = d.run(target=NEW, confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-noop-not-taken", "NO-OP" not in r.stdout, r.stdout[-400:])
        check("stripe-noop-exit-0", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-400:]}")
        check("stripe-noop-converged", d.container_env(NURSERY_KEY) == "1")

        again = d.run(target=NEW, confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-noop-idempotent",
              again.returncode == 0 and "NO-OP" in again.stdout,
              f"rc={again.returncode} {again.stdout[-300:]}")
    finally:
        d.cleanup()


def test_stripe_tax_runtime_unverified_fails():
    """Files converged but the flag never reached the PROCESS: the exact state
    that looks activated and still charges Odoo tax. Exit 10, and say that the
    MODULE promote succeeded so nobody rolls back the migration by reflex."""
    d = Droplet(env_blackhole=True)
    try:
        r = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-runtime-exit-10",
              r.returncode == 10
              and f"odoo process env has {NURSERY_KEY}=''" in r.stderr,
              f"rc={r.returncode} err={r.stderr[-500:]}")
        check("stripe-runtime-says-modules-ok",
              "module promote itself SUCCEEDED" in r.stderr, r.stderr[-400:])
        check("stripe-runtime-says-do-not-announce",
              "Do not announce the cutover" in r.stderr, r.stderr[-400:])
    finally:
        d.cleanup()


def test_stripe_tax_wrong_runtime_value_fails():
    """Something else is interpolating the flag (stale container, second env
    file). A promote that charged tax off a value nobody asked for is worse
    than a failed promote -- so this is exit 10, not a warning."""
    d = Droplet(env_blackhole=True, container_stripe_tax={"NURSERY": "0"})
    try:
        r = d.run(confirm="PROMOTE", extra_env={NURSERY_KEY: "1"})
        check("stripe-mismatch-exit-10",
              r.returncode == 10
              and f"odoo process env has {NURSERY_KEY}='0', not the requested '1'"
              in r.stderr,
              f"rc={r.returncode} err={r.stderr[-500:]}")
    finally:
        d.cleanup()


def test_stripe_tax_does_not_disturb_perenual():
    """The two converges share the compose patcher (GOL-2822 generalised it).
    Both in one run: one backup, both passthroughs, both in the process env,
    and the Perenual key still never echoed."""
    d = Droplet()
    try:
        r = d.run(confirm="PROMOTE",
                  extra_env={NURSERY_KEY: "1", "PERENUAL_API_KEY": FAKE_KEY})
        check("stripe-plus-perenual-exit-0", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-500:]}")
        check("stripe-plus-perenual-both-passthroughs",
              PASSTHROUGH in d.compose_text() and NURSERY_PASS in d.compose_text(),
              repr(d.compose_text()))
        check("stripe-plus-perenual-one-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
        check("stripe-plus-perenual-both-runtime",
              d.container_perenual() == FAKE_KEY
              and d.container_env(NURSERY_KEY) == "1")
        _no_leak("stripe-plus-perenual", r)
    finally:
        d.cleanup()


def test_stripe_tax_inert_passthrough_on_pre805_box_at_live_pin():
    """GOL-3209: the live prod droplet predates #805 -- no GROVE_STRIPE_TAX_*
    in .env OR the deployed compose -- and is already ON the promote target.
    The pre-flip prep is an all-tenants FALSEY converge at the CURRENT pin: it
    must add all three passthroughs (one compose backup), land exactly one
    `=0` line each, prove the process env carries them (inert = Odoo WV tax),
    and NOT take the NO-OP exit. A re-run is then the idempotent NO-OP, so the
    Train #3 flip is a pure value change with no compose edit."""
    tenants = ("GOLDBERRY", "GGG", "NURSERY")
    off = {f"GROVE_STRIPE_TAX_{t}": "0" for t in tenants}
    d = Droplet(env_ref=NEW, synced=NEW, marker=NEW)
    try:
        r = d.run(target=NEW, confirm="PROMOTE", extra_env=off)
        check("inert-prep-exit-0", r.returncode == 0,
              f"rc={r.returncode} {r.stderr[-500:]}")
        check("inert-prep-not-noop", "NO-OP" not in r.stdout, r.stdout[-400:])
        for t in tenants:
            key = f"GROVE_STRIPE_TAX_{t}"
            check(f"inert-prep-passthrough-{t}",
                  f"{key}: ${{{key}:-}}" in d.compose_text(),
                  repr(d.compose_text()))
            check(f"inert-prep-env-line-{t}",
                  d.env_text().count(f"{key}=") == 1
                  and f"{key}=0\n" in d.env_text(), repr(d.env_text()))
            check(f"inert-prep-runtime-{t}", d.container_env(key) == "0",
                  repr(d.container_env(key)))
        check("inert-prep-one-compose-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
        check("inert-prep-reports-odoo-tax",
              r.stdout.count("keeps Odoo's computed WV tax line") == 3,
              r.stdout[-600:])
        check("inert-prep-never-stripe", "now hands sales tax to STRIPE TAX"
              not in r.stdout, r.stdout[-400:])

        again = d.run(target=NEW, confirm="PROMOTE", extra_env=off)
        check("inert-prep-rerun-noop",
              again.returncode == 0 and "NO-OP" in again.stdout,
              f"rc={again.returncode} {again.stdout[-300:]}")
        check("inert-prep-rerun-no-new-backup", len(d.compose_backups()) == 1,
              str(d.compose_backups()))
    finally:
        d.cleanup()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(t.__name__)
        t()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
