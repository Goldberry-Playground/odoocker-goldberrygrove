#!/usr/bin/env python3
"""Regression tests for scripts/blogs-migrate.sh (legacy goldberry migration).

No network, no 1Password. The script runs against a stub `curl` that logs
every invocation and answers like Ghost's Admin API, and a stub `op` that
returns dummy credentials. Real `jq` is used.

The properties tested are the ones that broke the 2026-10-22 dry read:

  goldberry-old-side-by-ip   goldberrygrove.farm now resolves to the Next.js
                             storefront, so every OLD-side call for goldberry
                             must pin the legacy droplet with --resolve, and
                             carry -k (its Let's Encrypt cert expired
                             2025-10-07 once DNS moved and renewal failed).
  new-side-stays-strict      calls to blog.* never get -k or --resolve.
  hub-old-side-untouched     the hub tenant's old-side calls are unchanged.
  import-never-uploads-members  option C (2026-10-19): Odoo Farm News is the
                             list of record; no member writes into any Ghost.
                             members.csv is still exported, as a backup.
  bash-3.2-empty-opts        macOS /bin/bash 3.2 treats "${arr[@]}" of an
                             empty array as unbound under set -u; the hub
                             tenant (no old-side opts) must still run there.
  verify-prints-real-paths   the live legacy install is
                             /var/www/ghost_restored (/var/www/ghost is
                             empty); the blogs droplet needs the
                             grove-qa-admin key.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "blogs-migrate.sh")

LEGACY_RESOLVE = "goldberrygrove.farm:443:178.128.152.218"

# The stub logs each call as one JSON array of argv, then answers by URL.
STUB_CURL = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CURL_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
url = next((a for a in args if a.startswith("http")), "")
is_post = "POST" in args
if "/session/" in url:
    pass
elif "/members/export/" in url:
    sys.stdout.write("email,name\na@example.com,A\nb@example.com,B\n")
elif "/members/upload/" in url:
    sys.stdout.write('{"meta":{"stats":{"imported":2,"invalid":0}}}')
elif "/db/" in url and is_post:
    sys.stdout.write('{"problems":[]}')
elif "/db/" in url:
    sys.stdout.write('{"db":[{"data":{"posts":[{"slug":"a"},{"slug":"b"}]}}]}')
elif "?limit=1" in url:
    sys.stdout.write('{"meta":{"pagination":{"total":2}}}')
"""

STUB_OP = "#!/bin/sh\necho dummy\n"


class Run:
    def __init__(self, tenant, action, *, members_csv=None, bash="bash"):
        self.tmp = tempfile.mkdtemp(prefix="blogs-migrate-test-")
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        for name, body in (("curl", STUB_CURL), ("op", STUB_OP)):
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        if members_csv is not None:
            d = os.path.join(self.tmp, "blogs-migration", tenant)
            os.makedirs(d)
            with open(os.path.join(d, "export.json"), "w") as f:
                f.write('{"db":[{"data":{"posts":[]}}]}')
            with open(os.path.join(d, "members.csv"), "w") as f:
                f.write(members_csv)
        self.log = os.path.join(self.tmp, "curl.log")
        open(self.log, "w").close()
        env = dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"],
                   CURL_LOG=self.log)
        self.proc = subprocess.run([bash, SCRIPT, tenant, action], cwd=self.tmp,
                                   env=env, capture_output=True, text=True, timeout=60)
        with open(self.log) as f:
            self.calls = [json.loads(line) for line in f if line.strip()]

    @property
    def out(self):
        return self.proc.stdout + self.proc.stderr

    def calls_to(self, host_prefix):
        return [c for c in self.calls
                if any(a.startswith(host_prefix) for a in c)]

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _pins_legacy(call):
    return ("-k" in call and "--resolve" in call
            and call[call.index("--resolve") + 1] == LEGACY_RESOLVE)


def test_goldberry_old_side_reaches_legacy_box_by_ip():
    r = Run("goldberry", "export")
    try:
        assert r.proc.returncode == 0, r.out
        old = r.calls_to("https://goldberrygrove.farm/")
        assert len(old) >= 3, r.calls  # session, db export, members export
        assert all(_pins_legacy(c) for c in old), old
    finally:
        r.cleanup()


def test_new_side_stays_strict():
    r = Run("goldberry", "all")
    try:
        assert r.proc.returncode == 0, r.out
        new = r.calls_to("https://blog.goldberrygrove.farm/")
        assert new, r.calls
        assert not any("-k" in c or "--resolve" in c for c in new), new
    finally:
        r.cleanup()


def test_hub_old_side_untouched():
    r = Run("hub", "export")
    try:
        assert r.proc.returncode == 0, r.out
        old = r.calls_to("https://gatheringatthegrove.com/")
        assert old, r.calls
        assert not any("-k" in c or "--resolve" in c for c in old), old
    finally:
        r.cleanup()


def test_hub_runs_on_macos_bash_32():
    import pytest
    if not os.path.exists("/bin/bash"):
        pytest.skip("no /bin/bash")
    ver = subprocess.run(["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"],
                         capture_output=True, text=True).stdout.strip()
    if ver != "3":
        pytest.skip("/bin/bash is not 3.x")
    for action in ("export", "verify"):
        r = Run("hub", action, bash="/bin/bash")
        try:
            assert r.proc.returncode == 0, r.out
            assert "unbound variable" not in r.out, r.out
        finally:
            r.cleanup()


def test_import_never_uploads_members():
    r = Run("goldberry", "import", members_csv="email\na@example.com\n")
    try:
        assert r.proc.returncode == 0, r.out
        assert not any("/members/upload/" in a for c in r.calls for a in c), r.calls
        assert "option C" in r.out, r.out
    finally:
        r.cleanup()


def test_export_still_backs_up_members():
    r = Run("goldberry", "export")
    try:
        assert r.proc.returncode == 0, r.out
        with open(os.path.join(r.tmp, "blogs-migration/goldberry/members.csv")) as f:
            assert f.read().count("@example.com") == 2
    finally:
        r.cleanup()


def test_verify_prints_real_paths():
    r = Run("goldberry", "verify")
    try:
        assert r.proc.returncode == 0, r.out
        assert "root@178.128.152.218:/var/www/ghost_restored/content/images/" in r.out, r.out
        assert "-i ~/.ssh/grove-qa-admin" in r.out, r.out
        assert "root@159.203.131.236:/mnt/blogs-data/ghost-goldberry/images/" in r.out, r.out
        assert "/var/www/ghost/content" not in r.out, r.out
    finally:
        r.cleanup()
