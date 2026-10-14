#!/usr/bin/env python3
"""Regression tests for scripts/agent-git/github-push-doctor.sh (GOL-2571 / GOL-2629).

The doctor's whole job is to kill false "the broker is rejecting my key!"
escalations. Its HEALTHY block ends in a COPY-PASTE section, so that block is
not decoration -- it is the script's output contract. If a line in it is not
paste-correct, the doctor *manufactures* the exact false alarm it exists to
prevent.

That is what GOL-2629 caught: the raw-broker-mint line used `\\\\\\$(cat $KEYFILE)`
inside an UNQUOTED heredoc. `\\\\` emits `\\` and `\\$` emits `$`, so the printed
line was `Bearer \\$(cat ...)`; on paste, bash reads `\\$` inside double quotes as
an escaped literal `$`, so the header sent is the literal string `Bearer $(cat
...)` and the broker answers 401.

These tests pin BOTH halves of that contract, which a source-only grep cannot:

  emit-substitutable   the heredoc, rendered by a real bash, emits a mint line
                       whose header is `Bearer $(cat <keyfile>)` -- i.e. still
                       unexpanded, but substitutable -- never `Bearer \\$(cat`.
  paste-correct        feeding that emitted line back to bash (the user pasting
                       it) actually substitutes the keyfile contents into the
                       header, rather than sending a literal `$(cat ...)`.
  no-key-in-stdout     rendering the block never runs `cat` on the keyfile, so
                       no key material reaches the doctor's stdout.

No network and no broker: the heredoc body is lifted from the script source and
rendered by bash with a fake KEYFILE, so nothing is minted and nothing is
pushed.

    python3 scripts/test_github_push_doctor.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_DOCTOR = os.path.join(_HERE, "agent-git", "github-push-doctor.sh")

FAKE_KEY = "fake-broker-key-do-not-use-0123456789"
OWNER, REPO = "Goldberry-Playground", "odoocker-goldberrygrove"
BROKER = "http://gh-token-broker:9099"
HELPER = "/paperclip/agent-git/github-app-token.mjs"

_FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}" + (f"\n        {detail}" if detail else ""))
        _FAILURES.append(name)


# ── lift the HEALTHY copy-paste heredoc out of the script source ──────────────

def healthy_heredoc_body(src: str) -> str:
    """Return the body of the `cat <<EOF` block containing the HEALTHY banner.

    Deliberately source-structural rather than a hardcoded line number, so the
    test keeps testing the right block when the script is edited.
    """
    lines = src.splitlines()
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*cat\s*<<EOF\s*$", l)]
    for start in starts:
        end = next(
            (j for j in range(start + 1, len(lines)) if lines[j].rstrip() == "EOF"),
            None,
        )
        assert end is not None, f"unterminated heredoc opened at line {start + 1}"
        body = "\n".join(lines[start + 1 : end])
        if "PUSH PATH IS HEALTHY" in body:
            return body
    raise AssertionError("no `cat <<EOF` block containing the HEALTHY banner")


def render(body: str, keyfile: str) -> str:
    """Emit the heredoc exactly as the doctor does, via a real bash."""
    script = (
        "set -uo pipefail\n"
        f'BROKER={BROKER!r}\nHELPER={HELPER!r}\nKEYFILE={keyfile!r}\n'
        f'OWNER={OWNER!r}\nREPO={REPO!r}\n'
        "cat <<EOF\n" + body + "\nEOF\n"
    )
    out = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, f"rendering the heredoc failed: {out.stderr}"
    return out.stdout


def mint_line(rendered: str) -> str:
    for l in rendered.splitlines():
        if "Authorization: Bearer" in l:
            return l
    raise AssertionError(
        "the HEALTHY block emitted no `Authorization: Bearer` mint line:\n" + rendered
    )


def paste(line: str) -> str:
    """Simulate a user pasting the emitted line: run it, echoing the header."""
    # Turn the emitted `curl -s -H "<header>" \` into an echo of that header, so
    # we exercise bash's parse of the header string without any network at all.
    m = re.search(r'-H\s+("Authorization: Bearer [^"]*")', line)
    assert m, f"could not find a quoted Authorization header in: {line!r}"
    out = subprocess.run(
        ["bash", "-c", f"printf '%s' {m.group(1)}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, f"pasting the header failed: {out.stderr}"
    return out.stdout


def main() -> int:
    with open(_DOCTOR) as fh:
        src = fh.read()

    print("github-push-doctor.sh HEALTHY copy-paste block (GOL-2629)")

    body = healthy_heredoc_body(src)

    with tempfile.TemporaryDirectory() as td:
        keyfile = os.path.join(td, "gh-broker.key")
        with open(keyfile, "w") as fh:
            fh.write(FAKE_KEY + "\n")

        rendered = render(body, keyfile)
        line = mint_line(rendered)

        # 1. emit-substitutable --- the regression itself.
        check(
            "emit-substitutable: emitted header is `Bearer $(cat <keyfile>)`",
            f'"Authorization: Bearer $(cat {keyfile})"' in line,
            f"emitted line was: {line!r}",
        )
        check(
            "emit-substitutable: emitted header is NOT the escaped `Bearer \\$(cat`",
            "Bearer \\$(cat" not in line,
            "the heredoc over-escaped `$` (`\\\\\\$` in source emits `\\$`), so a "
            "pasted header is the literal string `$(cat ...)` and the broker 401s",
        )

        # 2. paste-correct --- what the user actually gets.
        pasted = paste(line)
        check(
            "paste-correct: pasting the emitted header substitutes the key",
            pasted == f"Authorization: Bearer {FAKE_KEY}",
            f"pasted header resolved to {pasted!r}, expected the keyfile contents",
        )
        check(
            "paste-correct: pasted header is not a literal `$(cat ...)`",
            "$(cat" not in pasted,
            f"pasted header resolved to {pasted!r}",
        )

        # 3. no-key-in-stdout --- `\$` must defer `cat` past emit time.
        check(
            "no-key-in-stdout: rendering never runs `cat` on the keyfile",
            FAKE_KEY not in rendered,
            "the heredoc expanded `$(cat $KEYFILE)` at emit time, so the doctor "
            "would print the live broker key to stdout",
        )

        # 4. the separate-param shape the mint line teaches (GOL-2571 false alarm #1).
        check(
            "mint line passes owner= and repo= as SEPARATE params",
            f"owner={OWNER}&repo={REPO}" in rendered,
            "a `?repo=<owner>/<repo>` shape leaves owner empty -> 403 "
            "owner_not_allowed, the very false alarm this script exists to kill",
        )

    if _FAILURES:
        print(f"\n{len(_FAILURES)} check(s) FAILED: " + ", ".join(_FAILURES))
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
