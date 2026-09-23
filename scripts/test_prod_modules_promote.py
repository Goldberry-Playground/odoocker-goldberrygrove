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
  money-guard-missing-line   grove_headless upgraded but no bind line at all
                             => non-zero exit, never a silent pass.
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
    # odoo: either the symlink-basename fallback or a manifest grep
    joined="${rest[*]}"
    case "$joined" in
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
                 auto_upgrade=True, upgrade_log=FULL_BIND, sync_fails=False):
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


def test_money_guard_missing_line():
    d = Droplet(upgrade_log="INFO odoo Modules loaded.\n")
    try:
        r = d.run(confirm="PROMOTE")
        check(
            "money-guard-missing-line",
            r.returncode == 8 and "NO \"WV 6% state sales tax bound for" in r.stderr,
            f"rc={r.returncode} err={r.stderr[-400:]}",
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
