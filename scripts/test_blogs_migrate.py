#!/usr/bin/env python3
"""Regression tests for scripts/blogs-migrate.sh (legacy goldberry migration).

No network, no 1Password. The script runs against a stub `curl` that logs
every invocation and answers like Ghost's Admin API, and a stub `op` that
returns dummy credentials. Real `jq` is used.

The properties tested are the ones that broke the 2026-10-22 dry read:

  goldberry-old-side-by-ip   goldberrygrove.farm now resolves to the Next.js
                             storefront, so every OLD-side call for goldberry
                             (export AND verify) must pin the legacy droplet
                             with --resolve and carry -k (its Let's Encrypt
                             cert expired 2025-10-07 once DNS moved), plus
                             --pinnedpubkey so -k cannot hand the owner
                             password to a substitute certificate.
  pin-required-fail-closed   without GOLDBERRY_LEGACY_PIN the script makes no
                             old-side call at all and says how to derive the
                             pin over SSH (trust = the SSH host key).
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
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(_HERE, "blogs-migrate.sh")

LEGACY_RESOLVE = "goldberrygrove.farm:443:178.128.152.218"
TEST_PIN = "dGVzdC1waW4tbm90LWEtcmVhbC1rZXktaGFzaC0wMDA="

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
    def __init__(self, tenant, action, *, members_csv=None, bash="bash", pin=TEST_PIN):
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
        env.pop("GOLDBERRY_LEGACY_PIN", None)
        if pin is not None:
            env["GOLDBERRY_LEGACY_PIN"] = pin
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
            and call[call.index("--resolve") + 1] == LEGACY_RESOLVE
            and "--pinnedpubkey" in call
            and call[call.index("--pinnedpubkey") + 1] == "sha256//" + TEST_PIN)


def test_goldberry_old_side_reaches_legacy_box_by_ip():
    # export: session + db + members; verify: session + 4 counts.
    for action, minimum in (("export", 3), ("verify", 5)):
        r = Run("goldberry", action)
        try:
            assert r.proc.returncode == 0, r.out
            old = r.calls_to("https://goldberrygrove.farm/")
            assert len(old) >= minimum, (action, r.calls)
            assert all(_pins_legacy(c) for c in old), (action, old)
        finally:
            r.cleanup()


def test_goldberry_without_pin_fails_closed():
    for action in ("export", "verify", "all"):
        r = Run("goldberry", action, pin=None)
        try:
            assert r.proc.returncode == 2, (action, r.out)
            assert not r.calls_to("https://goldberrygrove.farm/"), (action, r.calls)
            assert "GOLDBERRY_LEGACY_PIN" in r.out and "ssh root@178.128.152.218" in r.out, r.out
        finally:
            r.cleanup()


def test_goldberry_import_needs_no_pin():
    r = Run("goldberry", "import", members_csv="email\n", pin=None)
    try:
        assert r.proc.returncode == 0, r.out
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
    # Only bites on a Mac (CI runners have bash 5): returns early elsewhere.
    if not os.path.exists("/bin/bash"):
        return
    ver = subprocess.run(["/bin/bash", "-c", "echo ${BASH_VERSINFO[0]}"],
                         capture_output=True, text=True).stdout.strip()
    if ver != "3":
        return
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


def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = []
    for name, fn in tests:
        try:
            fn()
            print(f"ok    {name}")
        except Exception:  # noqa: BLE001 - report every failure, then exit 1
            failures.append(name)
            print(f"FAIL  {name}")
            traceback.print_exc()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + ", ".join(failures))
        return 1
    print(f"all {len(tests)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
