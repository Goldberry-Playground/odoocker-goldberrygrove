#!/usr/bin/env python3
"""Regression tests for the pgAdmin venv dist-info purge (GOL-2846).

No network, no docker. These parse `pgadmin/Dockerfile` and exercise its actual
`rm -rf` line under `/bin/sh` against a synthetic site-packages tree.

Why this exists — GOL-2846 (2026-09-30). `Trivy — Image Scan (pgadmin)` red on
main with 8 HIGH/CRITICAL findings against `urllib3 2.7.0` and `PyJWT 2.13.0`,
even though the pip block pins floors ABOVE both. Trivy decides a package is
"installed" from the presence of `<name>-<ver>.dist-info/METADATA` alone. The
Dockerfile's cleanup was keyed to the versions upstream bundled when each entry
was first written (`urllib3-2.6.3.dist-info`, `pyjwt-2.12.1.dist-info`), so when
upstream pgadmin moved to 2.7.0 / 2.13.0 those rm targets silently became
no-ops, the ORPHANED dist-info survived the pip install, and Trivy kept
reporting the superseded version forever. Raising the pin floor cannot fix that
class of failure — only version-globbing the dist-info removal can.

The three guards:

  distinfo_removals_are_globbed   no `*.dist-info` rm target carries a literal
                                  version, so an upstream bump can never orphan
                                  one again.
  every_pin_has_a_distinfo_purge  every distribution in the pip block has a
                                  matching dist-info glob — adding a pin without
                                  its purge line reintroduces the bug.
  rm_line_purges_orphans          the real rm line, run under POSIX sh, deletes
                                  the exact orphans GOL-2846 tripped on, leaves
                                  `pyasn1_modules` (a needed dependency whose
                                  name only PREFIXES `pyasn1`) intact, tolerates
                                  non-matching globs, and is idempotent.

    python3 scripts/test_pgadmin_distinfo_glob.py
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_DOCKERFILE = os.path.join(_ROOT, "pgadmin", "Dockerfile")

_SITE_PACKAGES_VAR = "PGADMIN_VENV_SITE_PACKAGES"

# pip requirement specifier -> the dist-info stem pip writes on disk. Only the
# entries whose on-disk name differs from the requirement name need listing;
# everything else normalises to itself.
_DISTINFO_STEM = {
    "python-socketio": "python_socketio",
    "python-engineio": "python_engineio",
}


def _read_dockerfile() -> str:
    with open(_DOCKERFILE, encoding="utf-8") as fh:
        return fh.read()


def _rm_line(text: str) -> str:
    """The single logical `RUN rm -rf ... && pip install` command, joined."""
    start = text.index("RUN rm -rf")
    # A backslash-continued shell command ends at the first newline whose
    # preceding line does not end in a continuation.
    lines = text[start:].splitlines()
    out = []
    for line in lines:
        out.append(line)
        if not line.rstrip().endswith("\\"):
            break
    return "\n".join(out)


def _rm_targets(text: str) -> list[str]:
    """Just the paths handed to `rm -rf`, before the `&&`."""
    cmd = _rm_line(text)
    cmd = cmd[: cmd.index("&&")]
    cmd = cmd.replace("\\\n", " ").replace("RUN rm -rf", "", 1)
    return [t.strip().strip('"') for t in cmd.split() if t.strip()]


def _pip_requirements(text: str) -> list[str]:
    """Distribution names from the quoted pip requirement specifiers."""
    block = text[text.index("pip3 install") :]
    block = block[: block.index("\n\n")] if "\n\n" in block else block
    names = []
    for spec in re.findall(r"'([A-Za-z0-9_.\-]+)[<>=!]", block):
        names.append(spec)
    return names


def test_distinfo_removals_are_globbed():
    targets = [t for t in _rm_targets(_read_dockerfile()) if t.endswith(".dist-info")]
    assert targets, "no .dist-info rm targets found — did the RUN block move?"
    pinned = [t for t in targets if not re.search(r"-\*\.dist-info$", t)]
    assert not pinned, (
        "these dist-info rm targets pin a literal version, so an upstream pgadmin "
        "bump will orphan them and Trivy will report the stale version forever "
        f"(GOL-2846): {pinned}. Use `<name>-*.dist-info` instead."
    )


def test_every_pin_has_a_distinfo_purge():
    text = _read_dockerfile()
    targets = _rm_targets(text)
    missing = []
    for name in _pip_requirements(text):
        stem = _DISTINFO_STEM.get(name, name)
        want = "${%s}/%s-*.dist-info" % (_SITE_PACKAGES_VAR, stem)
        # Case-insensitive: PyJWT/Pillow have varied in case upstream and the
        # Dockerfile lists both spellings.
        if not any(t.lower() == want.lower() for t in targets):
            missing.append(want)
    assert not missing, (
        "pip installs these distributions but never purges their bundled "
        f"dist-info, so Trivy will keep reporting the upstream version: {missing}"
    )


def test_rm_line_purges_orphans():
    text = _read_dockerfile()
    rm_only = _rm_line(text)
    rm_only = rm_only[: rm_only.index("&&")].replace("RUN ", "", 1)

    tmp = tempfile.mkdtemp(prefix="gol2846-")
    try:
        # Reproduce what upstream dpage/pgadmin4:9.16 actually had on disk when
        # GOL-2846 red'd, plus decoys that MUST survive.
        orphans = [
            "urllib3-2.7.0.dist-info",     # the real GOL-2846 orphan
            "pyjwt-2.13.0.dist-info",      # the real GOL-2846 orphan
            "Pillow-12.2.0.dist-info",     # capitalised upstream spelling
            "cryptography-49.0.0.dist-info",
            "sqlparse-0.5.5.dist-info",
            "pyasn1-0.6.3.dist-info",
            "urllib3", "jwt", "cryptography", "PIL", "pillow.libs",
            "pyasn1", "sqlparse",
        ]
        survivors = [
            # Depends on pyasn1>=0.4.6 and is itself unaffected; its name merely
            # PREFIXES pyasn1, so a sloppy `pyasn1*` glob would eat it.
            "pyasn1_modules",
            "pyasn1_modules-0.4.2.dist-info",
            "flask",
            "flask-3.1.0.dist-info",
        ]
        for d in orphans + survivors:
            os.makedirs(os.path.join(tmp, d), exist_ok=True)
        for d in orphans:
            if d.endswith(".dist-info"):
                open(os.path.join(tmp, d, "METADATA"), "w").close()

        env = dict(os.environ, **{_SITE_PACKAGES_VAR: tmp})
        for run in (1, 2):  # run twice: must be idempotent
            proc = subprocess.run(
                ["/bin/sh", "-c", rm_only],
                env=env, capture_output=True, text=True,
            )
            assert proc.returncode == 0, (
                f"rm line exited {proc.returncode} on run {run} "
                f"(non-matching globs must be tolerated): {proc.stderr.strip()}"
            )

        left = set(os.listdir(tmp))
        still_there = sorted(left & set(orphans))
        assert not still_there, (
            "orphaned dist-info / package dirs survived the purge, so Trivy will "
            f"still report them: {still_there}"
        )
        gone = sorted(set(survivors) - left)
        assert not gone, f"the purge deleted paths it must preserve: {gone}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
