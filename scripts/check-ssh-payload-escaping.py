#!/usr/bin/env python3
"""check-ssh-payload-escaping.py -- stop a remote SSH payload executing LOCALLY.

Our operator scripts hand the droplet one big DOUBLE-QUOTED string:

    ssh -o StrictHostKeyChecking=yes "${PROD_HOST}" "
      set -euo pipefail
      cd '${DEPLOY_DIR}'
      ...
    "

That shape is deliberate -- a few locals (${DEPLOY_DIR}, ${TARGET_REF}) must
expand HERE so the droplet receives literal values. But double quotes make the
WHOLE body an expansion context, so every un-backslashed `` ` `` and `$` in it
-- including ones sitting inside what a reader takes for a comment -- is
evaluated by the LOCAL shell at render time, before ssh is even invoked.

GOL-2531 (2026-09-23) is the live case. A comment added to
scripts/prod-modules-promote.sh read:

      # ... inspect it with `printenv` ...

Prose, backticks used as Markdown. Bash read them as command substitution, ran
`printenv` locally, and spliced the agent runtime's entire environment --
OP_SERVICE_ACCOUNT_TOKEN included -- into the payload string, which was then
printed. The token had to be rotated. An unescaped ${...} in the same block is
the worse half of the same bug: it interpolates a caller-side secret (e.g.
PERENUAL_API_KEY) into a file the payload writes on the droplet.

shellcheck cannot help here. SC2029 ("note that this expands locally") is the
only rule in the area and these scripts disable it on purpose, because local
expansion is the intent for the allowlisted vars. Nothing flags the accidents.

So this guard reads the payload the way bash does and enforces two rules:

  1. NO unescaped backtick and no unescaped `$(` inside the payload. Legit
     remote command substitution is always written \\$( ... ) in this repo;
     an unescaped one runs on the operator's laptop. Backticks are never
     correct here -- in prose escape them as \\` , in code use \\$( ).

  2. Every unescaped $VAR / ${VAR} must be a variable the SCRIPT ITSELF
     assigns. That is the definition of "intentional local expansion". A name
     the script never sets is either a typo (renders empty -- silent) or an
     ambient value inherited from the caller's environment, which is how a
     secret gets baked into a remote payload.

Remote-side variables stay correct with a backslash: \\$TARGET, \\"\\$AUTO\\".

Usage:
  python3 scripts/check-ssh-payload-escaping.py            # scans scripts/
  python3 scripts/check-ssh-payload-escaping.py FILE...    # scans given files
  python3 scripts/check-ssh-payload-escaping.py --selftest # runs the fixtures

Exit codes:
  0  no findings
  1  at least one payload would execute or interpolate locally
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIR = REPO_ROOT / "scripts"

# Names that are legitimately ambient rather than script-assigned. Keep this
# list empty-ish and justified: every entry is a hole in rule 2.
AMBIENT_ALLOWLIST: set[str] = set()

# ssh invoked with a double-quoted payload that does not close on the same
# line. Covers `ssh ...` and `/usr/bin/ssh ...`.
SSH_OPEN_RE = re.compile(r"(?:^|[;&|]\s*|\$\(\s*)(?:/\S*/)?ssh\s")

# The indirect shape scripts/caddy-prefer-prod-cert.sh uses:
#   SSH="ssh $SSH_OPTS root@$DROPLET_IP"
#   $SSH "<payload>"
# Same expansion hazard, so resolve the alias rather than silently skip it.
SSH_ALIAS_ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+|readonly\s+|local\s+)?([A-Za-z_][A-Za-z0-9_]*)="
    r"[\"']?(?:/\S*/)?ssh\s",
)

# VAR=, export VAR=, readonly VAR=, local VAR=, declare VAR=, VAR+=
ASSIGN_RE = re.compile(
    r"^\s*(?:export\s+|readonly\s+|local\s+|declare\s+(?:-\w+\s+)*)?"
    r"([A-Za-z_][A-Za-z0-9_]*)\+?=",
)
# for VAR in ... / while read VAR / read -r VAR
LOOPVAR_RE = re.compile(r"^\s*for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
READVAR_RE = re.compile(r"\bread\s+(?:-\w+\s+)*([A-Za-z_][A-Za-z0-9_]*)")
# : "${VAR:=default}" -- the assign-if-unset idiom
COLON_ASSIGN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):=")


class Finding:
    def __init__(self, path: Path, line: int, rule: str, detail: str, fix: str):
        self.path = path
        self.line = line
        self.rule = rule
        self.detail = detail
        self.fix = fix

    def render(self, root: Path) -> str:
        try:
            rel = self.path.relative_to(root)
        except ValueError:
            rel = self.path
        return (
            f"{rel}:{self.line}: [{self.rule}] {self.detail}\n"
            f"    fix: {self.fix}"
        )


def escaped_at(text: str, idx: int) -> bool:
    """True when text[idx] is preceded by an ODD run of backslashes."""
    n = 0
    j = idx - 1
    while j >= 0 and text[j] == "\\":
        n += 1
        j -= 1
    return n % 2 == 1


def count_open_quotes(line: str) -> int:
    """Unescaped double quotes on a line, ignoring single-quoted spans.

    Good enough for the payload-opening heuristic: we only need to know whether
    a line leaves a double quote hanging.
    """
    total = 0
    in_single = False
    for i, ch in enumerate(line):
        if ch == "'" and not escaped_at(line, i) and not in_single:
            in_single = True
        elif ch == "'" and in_single:
            in_single = False
        elif ch == '"' and not in_single and not escaped_at(line, i):
            total += 1
    return total


def assigned_names(lines: list[str]) -> set[str]:
    names: set[str] = set()
    for line in lines:
        m = ASSIGN_RE.match(line)
        if m:
            names.add(m.group(1))
        m = LOOPVAR_RE.match(line)
        if m:
            names.add(m.group(1))
        for m in READVAR_RE.finditer(line):
            names.add(m.group(1))
        for m in COLON_ASSIGN_RE.finditer(line):
            names.add(m.group(1))
    return names


def find_payloads(lines: list[str]) -> list[tuple[int, int]]:
    """Return (start_idx, end_idx) line spans of multi-line ssh payloads.

    start_idx is the line AFTER the opening quote; end_idx is exclusive and
    points at the line holding the closing quote.
    """
    aliases = {
        m.group(1) for m in (SSH_ALIAS_ASSIGN_RE.match(ln) for ln in lines) if m
    }
    alias_re = (
        re.compile(
            r"(?:^|[;&|]\s*)\$\{?(" + "|".join(sorted(map(re.escape, aliases))) + r")\}?\s"
        )
        if aliases
        else None
    )

    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        is_open = SSH_OPEN_RE.search(line) or (
            alias_re is not None and alias_re.search(line)
        )
        if not stripped.startswith("#") and is_open:
            if count_open_quotes(line) % 2 == 1:
                j = i + 1
                while j < len(lines) and count_open_quotes(lines[j]) % 2 == 0:
                    j += 1
                if j < len(lines):
                    spans.append((i + 1, j))
                    i = j
        i += 1
    return spans


def scan_payload(
    path: Path, lines: list[str], start: int, end: int, known: set[str]
) -> list[Finding]:
    findings: list[Finding] = []
    for n in range(start, end):
        line = lines[n]
        lineno = n + 1
        for i, ch in enumerate(line):
            if escaped_at(line, i):
                continue
            if ch == "`":
                findings.append(
                    Finding(
                        path,
                        lineno,
                        "local-command-substitution",
                        "unescaped backtick inside an ssh payload -- the LOCAL "
                        "shell runs it at render time, even in a comment",
                        "escape it as \\` for prose, or use \\$( ... ) for a "
                        "command that must run on the droplet",
                    )
                )
            elif ch == "$" and line[i + 1 : i + 2] == "(":
                findings.append(
                    Finding(
                        path,
                        lineno,
                        "local-command-substitution",
                        "unescaped $( inside an ssh payload -- the LOCAL shell "
                        "runs it at render time",
                        "escape it as \\$( so it runs on the droplet",
                    )
                )
            elif ch == "$":
                rest = line[i + 1 :]
                m = re.match(r"\{([A-Za-z_][A-Za-z0-9_]*)", rest) or re.match(
                    r"([A-Za-z_][A-Za-z0-9_]*)", rest
                )
                if not m:
                    continue
                name = m.group(1)
                if name in known or name in AMBIENT_ALLOWLIST:
                    continue
                findings.append(
                    Finding(
                        path,
                        lineno,
                        "ambient-local-expansion",
                        f"${{{name}}} expands LOCALLY but this script never "
                        f"assigns {name} -- it is a typo (renders empty) or an "
                        f"inherited environment value baked into the payload",
                        f"escape it as \\${name} to evaluate on the droplet, or "
                        f"assign {name} locally so the expansion is deliberate",
                    )
                )
    return findings


def check_file(path: Path) -> list[Finding]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    known = assigned_names(lines)
    findings: list[Finding] = []
    for start, end in find_payloads(lines):
        findings.extend(scan_payload(path, lines, start, end, known))
    return findings


SELFTEST_BAD = """#!/usr/bin/env bash
HOST=example
ssh "$HOST" "
  set -eu
  # inspect it with `printenv` first
  echo ${SOME_AMBIENT_TOKEN}
  echo $(hostname)
  echo \\"\\$REMOTE\\"
"
"""

SELFTEST_GOOD = """#!/usr/bin/env bash
HOST=example
DEPLOY_DIR=/etc/grove
ssh "$HOST" "
  set -eu
  # inspect it with \\`printenv\\` first
  cd '${DEPLOY_DIR}'
  echo \\"\\$(hostname)\\"
  echo \\"\\$REMOTE\\"
"
"""


def selftest() -> int:
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory() as td:
        bad = Path(td) / "bad.sh"
        bad.write_text(SELFTEST_BAD, encoding="utf-8")
        good = Path(td) / "good.sh"
        good.write_text(SELFTEST_GOOD, encoding="utf-8")

        bad_rules = sorted({f.rule for f in check_file(bad)})
        expected = ["ambient-local-expansion", "local-command-substitution"]
        if bad_rules != expected:
            print(f"SELFTEST FAIL: bad fixture rules {bad_rules} != {expected}")
            ok = False

        bad_lines = sorted({f.line for f in check_file(bad)})
        if bad_lines != [5, 6, 7]:
            print(f"SELFTEST FAIL: bad fixture lines {bad_lines} != [5, 6, 7]")
            ok = False

        good_findings = check_file(good)
        if good_findings:
            print("SELFTEST FAIL: good fixture produced findings:")
            for f in good_findings:
                print("  " + f.render(Path(td)))
            ok = False

    if ok:
        print("selftest OK (bad fixture caught on 3 lines, good fixture clean)")
        return 0
    return 1


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()

    if argv:
        targets = [Path(a).resolve() for a in argv]
    else:
        targets = sorted(SCAN_DIR.rglob("*.sh"))

    findings: list[Finding] = []
    scanned = 0
    for path in targets:
        if not path.is_file():
            continue
        scanned += 1
        findings.extend(check_file(path))

    if findings:
        print(
            f"ERROR: {len(findings)} ssh payload(s) would execute or "
            f"interpolate on the LOCAL machine at render time.\n"
        )
        for f in findings:
            print(f.render(REPO_ROOT))
        print(
            "\nWhy this is a stop-ship: the payload is a double-quoted string, "
            "so bash expands it\nbefore ssh runs. GOL-2531 leaked a 1Password "
            "service-account token exactly this way,\nvia backticks inside a "
            "COMMENT. Never dump a rendered payload from a shell that holds\n"
            "secrets -- inspect it with `bash -n` or this guard instead."
        )
        return 1

    print(f"OK: {scanned} shell script(s) scanned, no locally-expanding ssh payloads")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
