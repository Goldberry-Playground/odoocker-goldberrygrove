#!/usr/bin/env python3
"""Regression tests for scripts/prod-odoo-image-apply.sh.

NO NETWORK, NO SSH, NO DOCKER. `ssh`, `docker` and `sleep` are stubbed on PATH:
the ssh stub runs the remote program in a local bash, and the docker stub keeps
a tiny fake daemon (tag -> image id, image id -> digest, the running image) in
a state dir, with a fake image filesystem for the addon gate.

What these pin:

  check-is-read-only       check mode reports PENDING (exit 2) on a box whose
                           .env still says latest and lacks third-party-addons,
                           and writes nothing.
  apply-happy-path         --apply pins ODOO_TAG, inserts third-party-addons
                           between the community addons and /workspace/current,
                           keeps a .env backup and a rollback image tag, runs
                           the -u pass, and a second run is a no-op.
  refuses-wrong-digest     a tag that resolves to another digest than the one
                           QA was gated on => REFUSED, exit 2, .env untouched.
  refuses-missing-addon    image without a required OCA addon => REFUSED.
  dirty-upgrade-rolls-back an ERROR line (or a non-zero exit) in the -u pass
                           puts the box back on the old image via the rollback
                           tag, exit 10.
  unhealthy-rolls-back     new image never answers /web/login => rolled back.
  local validation         --apply needs a 40-char sha and a sha256 digest;
                           `latest` is rejected before any ssh.

Run:  python3 scripts/test_prod_odoo_image_apply.py
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "prod-odoo-image-apply.sh")
REPO = "ghcr.io/goldberry-playground/grove-odoo"
SHA = "b" * 40
OLD_ID = "sha256:" + "1" * 64
NEW_ID = "sha256:" + "2" * 64
OLD_DIGEST = "sha256:" + "c" * 64
NEW_DIGEST = "sha256:" + "d" * 64
COMMUNITY = "/usr/lib/python3/dist-packages/odoo/addons"
THIRD = "/usr/lib/python3/dist-packages/odoo/third-party-addons"
ADDONS = ("account_reconcile_oca", "account_statement_import_sheet_file")

ENV = f"""APP_ENV=production
ODOO_TAG=latest
DB_NAME=grove
ADDONS_PATH={COMMUNITY},/workspace/current
WORKERS=4
"""

CLEAN_LOG = """2026-12-02 15:00:01,000 1 INFO grove odoo.modules.loading: loading 148 modules...
2026-12-02 15:04:01,000 1 WARNING grove odoo.fields: something cosmetic
2026-12-02 15:05:01,000 1 INFO grove odoo.modules.loading: Modules loaded.
"""
DIRTY_LOG = CLEAN_LOG + "2026-12-02 15:05:02,000 1 ERROR grove odoo.modules.registry: boom\n"

SSH_STUB = """#!/usr/bin/env bash
# ignore host/options; run the program arriving on stdin locally
exec bash -s
"""

SLEEP_STUB = """#!/usr/bin/env bash
exit 0
"""

DOCKER_STUB = r"""#!/usr/bin/env bash
# Fake daemon state in $STUB_STATE: tags (ref id), digests (id digest), running (id).
S="$STUB_STATE"
echo "$*" >> "$S/calls"
resolve() {  # ref or id -> id
  case "$1" in sha256:*) echo "$1"; return 0 ;; esac
  awk -v r="$1" '$1==r {id=$2} END {if (id) print id}' "$S/tags"
}
if [ "$1" != compose ]; then
  case "$1" in
    pull)
      [ "$2" = -q ] && shift
      [ -n "$(resolve "$2")" ] || exit 1
      exit 0 ;;
    inspect) cat "$S/running"; exit 0 ;;          # docker inspect --format .. <cid>
    image)                                        # docker image inspect --format .. <ref|id>
      id="$(resolve "$5")"; [ -n "$id" ] || exit 1
      awk -v i="$id" -v repo="$STUB_REPO" '$1==i {printf "%s@%s ", repo, $2}' "$S/digests"; echo
      exit 0 ;;
    tag) echo "$3 $2" >> "$S/tags"; exit 0 ;;
    run)                                          # docker run --rm --entrypoint test <ref> -f|-d <path>
      id="$(resolve "$5")"
      [ "$id" = "$STUB_NEW_ID" ] || exit 1
      test "$6" "$STUB_ROOT$7"; exit $? ;;
  esac
  echo "stub-docker: unhandled: $*" >&2; exit 98
fi
shift
[ "$1" = --env-file ] && { ENVF="$2"; shift 2; }
running="$(cat "$S/running" 2>/dev/null || true)"
case "$1" in
  ps) [ -n "$running" ] && echo cid1; exit 0 ;;
  config) exit 0 ;;
  logs) echo "fake log line"; exit 0 ;;
  restart) echo restart >> "$S/events"; exit 0 ;;
  up)
    tag="$(sed -n 's/^ODOO_TAG=//p' "$ENVF" | head -1)"
    id="$(resolve "$STUB_REPO:$tag")"
    [ -n "$id" ] || { echo "stub-docker: no image for tag $tag" >&2; exit 1; }
    echo "$id" > "$S/running"
    echo "addons_path = $(sed -n 's/^ADDONS_PATH=//p' "$ENVF" | head -1)" > "$S/odoo.conf"
    echo "up $tag" >> "$S/events"
    exit 0 ;;
  exec)
    shift; [ "$1" = -T ] && shift; shift   # drop service name
    case "$1" in
      python3)  # health probe
        if [ "$running" = "$STUB_NEW_ID" ] && [ "${STUB_NEW_UNHEALTHY:-0}" = 1 ]; then exit 1; fi
        exit 0 ;;
      sh) ODOO_RC="$S/odoo.conf" sh -c "$3"; exit $? ;;
      odoo)
        case "$2" in
          --version) [ "$running" = "$STUB_NEW_ID" ] && echo "Odoo Server 19.0-20261102" || echo "Odoo Server 19.0-20260513"; exit 0 ;;
          shell) cat >/dev/null; echo "24 | enabled | At The Grove Nursery, LLC | set"; exit 0 ;;
          -d)
            echo "upgrade $*" >> "$S/events"
            cat "$STUB_UPGRADE_LOG" >&2
            exit "${STUB_UPGRADE_RC:-0}" ;;
        esac ;;
    esac ;;
esac
echo "stub-docker: unhandled compose: $*" >&2
exit 98
"""


class Box:
    """A fake droplet: deploy dir with .env, fake docker state, stubbed PATH."""

    def __init__(self, tmp: str, present=ADDONS, new_digest=NEW_DIGEST):
        self.tmp = tmp
        self.deploy = os.path.join(tmp, "grove")
        os.makedirs(self.deploy)
        self.env = os.path.join(self.deploy, ".env")
        with open(self.env, "w") as f:
            f.write(ENV)
        with open(os.path.join(self.deploy, "docker-compose.yml"), "w") as f:
            f.write("services:\n  odoo: {}\n")
        self.state = os.path.join(tmp, "state")
        os.makedirs(self.state)
        self._write("tags", f"{REPO}:latest {OLD_ID}\n{REPO}:{SHA} {NEW_ID}\n")
        self._write("digests", f"{OLD_ID} {OLD_DIGEST}\n{NEW_ID} {new_digest}\n")
        self._write("running", OLD_ID + "\n")
        self._write("odoo.conf", f"addons_path = {COMMUNITY},/workspace/current\n")
        self.root = os.path.join(tmp, "root")
        os.makedirs(self.root + THIRD)
        for a in present:
            os.makedirs(os.path.join(self.root + THIRD, a))
            open(os.path.join(self.root + THIRD, a, "__manifest__.py"), "w").close()
        self.clean = os.path.join(tmp, "clean.log")
        self.dirty = os.path.join(tmp, "dirty.log")
        with open(self.clean, "w") as f:
            f.write(CLEAN_LOG)
        with open(self.dirty, "w") as f:
            f.write(DIRTY_LOG)
        self.bin = os.path.join(tmp, "bin")
        os.makedirs(self.bin)
        for name, body in (("ssh", SSH_STUB), ("docker", DOCKER_STUB), ("sleep", SLEEP_STUB)):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)

    def _write(self, name, text):
        with open(os.path.join(self.state, name), "w") as f:
            f.write(text)

    def read(self, name):
        p = os.path.join(self.state, name)
        if not os.path.exists(p):
            return ""
        with open(p) as f:
            return f.read()

    def run(self, *args, **env):
        e = dict(os.environ)
        for k in ("IMAGE_TAG", "EXPECT_DIGEST", "PROD_HOST", "SKIP_UPGRADE", "UPGRADE_MODULES",
                  "MAX_ERROR_LINES", "KEEP_ON_FAILURE", "FORCE_UPGRADE", "REQUIRE_ADDONS", "THIRD_PARTY_DIR"):
            e.pop(k, None)
        e.update(
            PATH=self.bin + os.pathsep + e["PATH"],
            DEPLOY_DIR=self.deploy,
            HEALTH_TIMEOUT="10",
            STUB_STATE=self.state,
            STUB_ROOT=self.root,
            STUB_REPO=REPO,
            STUB_NEW_ID=NEW_ID,
            STUB_UPGRADE_LOG=self.clean,
        )
        e.update(env)
        return subprocess.run(["bash", SCRIPT, *args], env=e, capture_output=True, text=True)

    def env_text(self):
        with open(self.env) as f:
            return f.read()

    def backups(self):
        return [f for f in os.listdir(self.deploy) if f.startswith(".env.bak.")]

    def rollback_tags(self):
        return [l.split()[0] for l in self.read("tags").splitlines() if ":rollback-" in l]


class Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_check_is_read_only(self):
        box = Box(self.tmp)
        r = box.run(IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("RESULT: PENDING", out)
        self.assertIn("ADDONS_PATH carries third-party-addons: no", out)
        self.assertIn("24 | enabled | At The Grove Nursery, LLC | set", out)
        self.assertEqual(box.env_text(), ENV)
        self.assertEqual(box.backups(), [])
        self.assertEqual(box.read("events"), "")
        self.assertNotIn("pull", box.read("calls"))

    def test_check_without_a_target_still_reports(self):
        box = Box(self.tmp)
        r = box.run()
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn(".env ODOO_TAG     = latest", r.stdout)

    def test_apply_happy_path_then_noop(self):
        box = Box(self.tmp)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("RESULT: applied and verified", out)
        text = box.env_text()
        self.assertIn(f"ODOO_TAG={SHA}\n", text)
        self.assertIn(f"ADDONS_PATH={COMMUNITY},{THIRD},/workspace/current\n", text)
        self.assertIn("WORKERS=4\n", text)
        self.assertEqual(len(box.backups()), 1)
        with open(os.path.join(box.deploy, box.backups()[0])) as f:
            self.assertEqual(f.read(), ENV)
        self.assertEqual(len(box.rollback_tags()), 1)
        events = box.read("events").splitlines()
        self.assertEqual(events[0], f"up {SHA}")
        self.assertIn("-u all --stop-after-init --no-http --workers=0", events[1])
        self.assertEqual(events[2], "restart")
        self.assertEqual(box.read("running").strip(), NEW_ID)
        self.assertIn("0 CRITICAL, 0 ERROR, 1 WARNING", out)
        self.assertIn("PITR restore point", out)
        self.assertIn("Rollback by hand", out)

        r2 = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST)
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("converged, nothing to do", r2.stdout)
        self.assertEqual(len(box.backups()), 1)
        self.assertEqual(len(box.read("events").splitlines()), 3)

        r3 = box.run(IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST)
        self.assertEqual(r3.returncode, 0, r3.stdout + r3.stderr)

        # QA rehearsal: box already on the pinned image, DB still needs the pass
        r4 = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST, FORCE_UPGRADE="1")
        self.assertEqual(r4.returncode, 0, r4.stdout + r4.stderr)
        self.assertIn("-u all", box.read("events").splitlines()[4])
        self.assertIn(f"ADDONS_PATH={COMMUNITY},{THIRD},/workspace/current\n", box.env_text())

    def test_skip_upgrade_swaps_image_only(self):
        box = Box(self.tmp)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST, SKIP_UPGRADE="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(box.read("events").splitlines(), [f"up {SHA}"])

    def test_refuses_wrong_digest(self):
        box = Box(self.tmp, new_digest="sha256:" + "e" * 64)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        self.assertIn("RESULT: REFUSED", out)
        self.assertIn("not the image the QA gate ran on", out)
        self.assertEqual(box.env_text(), ENV)
        self.assertEqual(box.backups(), [])
        self.assertEqual(box.read("events"), "")

    def test_refuses_missing_addon(self):
        box = Box(self.tmp, present=ADDONS[:1])
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST)
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 2, out)
        self.assertIn(f"lacks: {ADDONS[1]}", out)
        self.assertEqual(box.env_text(), ENV)
        self.assertEqual(box.read("events"), "")

    def _assert_rolled_back(self, box, r):
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 10, out)
        self.assertIn("RESULT: FAILED", out)
        self.assertIn("rolled back: odoo serves on the previous image", out)
        tag = box.rollback_tags()[0].split(":")[-1]
        text = box.env_text()
        self.assertIn(f"ODOO_TAG={tag}\n", text)
        self.assertIn(f"ADDONS_PATH={COMMUNITY},/workspace/current\n", text)
        self.assertEqual(box.read("running").strip(), OLD_ID)
        self.assertEqual(box.read("events").splitlines()[-1], f"up {tag}")

    def test_dirty_upgrade_rolls_back(self):
        box = Box(self.tmp)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST, STUB_UPGRADE_LOG=box.dirty)
        self._assert_rolled_back(box, r)
        self.assertIn("ERROR grove odoo.modules.registry: boom", r.stdout)
        self.assertIn("PITR point", r.stderr)

    def test_nonzero_upgrade_exit_rolls_back(self):
        box = Box(self.tmp)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST, STUB_UPGRADE_RC="1")
        self._assert_rolled_back(box, r)

    def test_max_error_lines_tolerates_known_noise(self):
        box = Box(self.tmp)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST,
                    STUB_UPGRADE_LOG=box.dirty, MAX_ERROR_LINES="1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_unhealthy_new_image_rolls_back(self):
        box = Box(self.tmp)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST, STUB_NEW_UNHEALTHY="1")
        self._assert_rolled_back(box, r)
        self.assertNotIn("upgrade", box.read("events"))

    def test_keep_on_failure_leaves_new_image(self):
        box = Box(self.tmp)
        r = box.run("--apply", IMAGE_TAG=SHA, EXPECT_DIGEST=NEW_DIGEST,
                    STUB_UPGRADE_LOG=box.dirty, KEEP_ON_FAILURE="1")
        self.assertEqual(r.returncode, 10, r.stdout + r.stderr)
        self.assertEqual(box.read("running").strip(), NEW_ID)
        self.assertIn(f"ODOO_TAG={SHA}\n", box.env_text())

    def test_local_validation(self):
        box = Box(self.tmp)
        for env in (
            {},                                                   # nothing given
            {"IMAGE_TAG": SHA},                                   # no digest
            {"IMAGE_TAG": "latest", "EXPECT_DIGEST": NEW_DIGEST},  # moving tag
            {"IMAGE_TAG": SHA, "EXPECT_DIGEST": "sha256:short"},
            {"IMAGE_TAG": SHA, "EXPECT_DIGEST": NEW_DIGEST, "UPGRADE_MODULES": "all; rm -rf /"},
        ):
            r = box.run("--apply", **env)
            self.assertEqual(r.returncode, 3, f"{env}: {r.stdout}{r.stderr}")
        self.assertEqual(box.env_text(), ENV)
        self.assertEqual(box.read("calls"), "")

    def test_default_host_is_the_ssh_alias(self):
        box = Box(self.tmp)
        r = box.run()
        self.assertIn(f">> target grove-prod-odoo:{box.deploy}", r.stdout, r.stdout + r.stderr)


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False, verbosity=1).result.wasSuccessful() else 1)
