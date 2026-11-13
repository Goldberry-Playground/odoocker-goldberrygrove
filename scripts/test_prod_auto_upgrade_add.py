#!/usr/bin/env python3
"""Regression tests for scripts/prod-auto-upgrade-add.sh.

NO NETWORK, NO SSH, NO DOCKER. `ssh`, `docker` and `gh` are stubbed on PATH:
the ssh stub runs the remote program in a local bash, the docker stub answers
the handful of `docker compose` calls the script makes against a fake addons
tree, and the gh stub answers the manifest probe.

What these pin:

  gate-finds-deep-addons   addons at the image's real depth
                           (<root>/usr/lib/python3/dist-packages/odoo/
                           third-party-addons/<addon>, 7 levels) pass gate 2.
                           The first version used `find / -maxdepth 5`, which
                           never reached them and refused on a correct image.
  gate-refuses-missing     one required addon absent => REFUSED, exit 2, and
                           the deployed compose is byte-for-byte unchanged.
  apply-appends-once       --apply appends the module, keeps a backup, and a
                           second run is a no-op.
  apply-needs-target-ref   --apply without TARGET_REF exits 3 before any ssh.
  gate1-refuses-bad-ref    manifest absent at TARGET_REF exits 3.

Run:  python3 scripts/test_prod_auto_upgrade_add.py
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "prod-auto-upgrade-add.sh")
ADDONS = ("account_reconcile_oca", "account_statement_import_sheet_file")
SHA = "a" * 40

COMPOSE = """services:
  odoo:
    environment:
      APP_ENV: production
      AUTO_UPGRADE_MODULES: grove_headless,grove_support,grove_calendar
"""

SSH_STUB = """#!/usr/bin/env bash
# ignore host/options; run the program arriving on stdin locally
exec bash -s
"""

GH_STUB = """#!/usr/bin/env bash
[ "${STUB_GH_OK:-1}" = 1 ] && { echo grove_schedule_f/__manifest__.py; exit 0; }
exit 1
"""

DOCKER_STUB = r"""#!/usr/bin/env bash
# docker compose --env-file <f> <subcommand...>
[ "$1" = compose ] || { echo "stub-docker: not compose: $*" >&2; exit 98; }
shift
[ "$1" = --env-file ] && shift 2
case "$1" in
  images) echo "CONTAINER REPOSITORY TAG"; echo "odoo grove-odoo latest"; exit 0 ;;
  config) exit 0 ;;
  up)     echo "recreate" >> "$STUB_LOG"; exit 0 ;;
  exec)
    shift; [ "$1" = -T ] && shift; shift   # drop service name
    case "$1" in
      true) exit 0 ;;
      test) shift; test "$@"; exit $? ;;
      printenv)
        if [ "$2" = AUTO_UPGRADE_MODULES ]; then
          grep -E '^[[:space:]]*AUTO_UPGRADE_MODULES:' "$STUB_COMPOSE" | cut -d: -f2- | tr -d ' '
          exit 0
        fi
        exit 1 ;;
      sh) shift; shift; ADDONS_PATH="$STUB_ADDONS_PATH" sh -c "$1"; exit $? ;;
    esac ;;
esac
echo "stub-docker: unhandled: $*" >&2
exit 98
"""


class Box:
    """A fake droplet: deploy dir, addons tree, stubbed PATH."""

    def __init__(self, tmp: str, present=ADDONS):
        self.deploy = os.path.join(tmp, "grove")
        os.makedirs(self.deploy)
        self.compose = os.path.join(self.deploy, "docker-compose.yml")
        with open(self.compose, "w") as f:
            f.write(COMPOSE)
        open(os.path.join(self.deploy, ".env"), "w").close()
        root = os.path.join(tmp, "root", "usr", "lib", "python3", "dist-packages", "odoo")
        self.community = os.path.join(root, "addons")
        self.third = os.path.join(root, "third-party-addons")
        os.makedirs(self.community)
        os.makedirs(self.third)
        for a in present:
            os.makedirs(os.path.join(self.third, a))
            open(os.path.join(self.third, a, "__manifest__.py"), "w").close()
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("ssh", SSH_STUB), ("docker", DOCKER_STUB), ("gh", GH_STUB)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.log = os.path.join(tmp, "stub.log")

    def run(self, *args, **env):
        e = dict(os.environ)
        e.update(
            PATH=self.bin + os.pathsep + e["PATH"],
            DEPLOY_DIR=self.deploy,
            STUB_COMPOSE=self.compose,
            STUB_LOG=self.log,
            STUB_ADDONS_PATH=f"{self.community},{self.third}",
        )
        e.pop("TARGET_REF", None)
        e.update(env)
        return subprocess.run(["bash", SCRIPT, *args], env=e, capture_output=True, text=True)

    def compose_text(self):
        with open(self.compose) as f:
            return f.read()

    def backups(self):
        return [f for f in os.listdir(self.deploy) if f.startswith("docker-compose.yml.bak.")]


class Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_gate_finds_deep_addons(self):
        box = Box(self.tmp)
        r = box.run()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        for a in ADDONS:
            self.assertIn(f"gate 2 OK: {a} at {box.third}/{a}", out)
        self.assertIn("append PENDING", out)
        self.assertNotIn("REFUSED", out)
        self.assertEqual(box.compose_text(), COMPOSE)

    def test_gate_refuses_missing(self):
        box = Box(self.tmp, present=ADDONS[:1])
        r = box.run("--apply", TARGET_REF=SHA)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        self.assertIn(f"REFUSED. The running image lacks: {ADDONS[1]}", out)
        self.assertEqual(box.compose_text(), COMPOSE)
        self.assertEqual(box.backups(), [])

    def test_apply_appends_once(self):
        box = Box(self.tmp)
        r = box.run("--apply", TARGET_REF=SHA)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn(
            "AUTO_UPGRADE_MODULES: grove_headless,grove_support,grove_calendar,grove_schedule_f\n",
            box.compose_text(),
        )
        self.assertEqual(len(box.backups()), 1)
        with open(box.log) as f:
            self.assertIn("recreate", f.read())
        self.assertIn("applied and verified", out)

        r2 = box.run("--apply", TARGET_REF=SHA)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("already in AUTO_UPGRADE_MODULES", r2.stdout)
        self.assertEqual(box.compose_text().count("grove_schedule_f"), 1)
        self.assertEqual(len(box.backups()), 1)

    def test_no_recreate_skips_the_recreate(self):
        box = Box(self.tmp)
        r = box.run("--apply", TARGET_REF=SHA, NO_RECREATE="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("grove_schedule_f", box.compose_text())
        self.assertFalse(os.path.exists(box.log))

    def test_apply_needs_target_ref(self):
        box = Box(self.tmp)
        r = box.run("--apply")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(box.compose_text(), COMPOSE)

    def test_gate1_refuses_bad_ref(self):
        box = Box(self.tmp)
        r = box.run("--apply", TARGET_REF=SHA, STUB_GH_OK="0")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("is NOT at", r.stderr)
        self.assertEqual(box.compose_text(), COMPOSE)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
