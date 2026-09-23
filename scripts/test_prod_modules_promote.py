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
  up)
    # up -d --force-recreate --no-deps custom-modules-sync => adopt the .env ref
    if [ -f "$S/sync_fails" ]; then exit 0; fi
    sed -n 's/^CUSTOM_MODULES_REF=//p' "$S/envfile" | tail -1 | tr -d '\r' > "$S/synced"
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
                 recorded_version=VER_BELOW_TAX_MIGRATION, tax_out=TAX_ALL_OK):
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

        with open(os.path.join(self.deploy, "docker-compose.yml"), "w") as fh:
            fh.write(COMPOSE_WITH_AUTO if auto_upgrade else COMPOSE_WITHOUT_AUTO)

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
