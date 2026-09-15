#!/usr/bin/env python3
"""
reconcile_modules_pin — rewrite the committed prod ``custom_modules_ref`` default
to match a live grove-odoo-modules git-sync pin, and append a one-line note to
its description in the same running-log style as every prior manual bump.

WHY THIS EXISTS
---------------
Prod's grove-odoo-modules pin is carried in two places that drift apart:

  * the RUNNING pin — ``/etc/grove/.env``'s ``CUSTOM_MODULES_REF`` on the prod
    droplet, hand-edited during incidents (``ignore_changes`` keeps
    ``terraform apply`` off the box), so it can move without any commit; and
  * the COMMITTED default — ``var.custom_modules_ref`` in
    ``infra/terraform/environments/production/variables.tf``, which the NEXT
    droplet rebuild would roll prod back onto unless it already matches.

GOL-2280 was the third manual reconcile of that gap in a week. Each one is the
same mechanical edit: change the 40-hex default and append a dated note saying
"converge the committed default onto the live pin, drift-only, no roll-forward".
This script IS that edit, so ``.github/workflows/reconcile-modules-pin.yml`` can
run it on a ``workflow_dispatch`` and the ssh pin recipe ends with one
``gh workflow run`` line instead of a hand-edited PR.

DESIGN — narrow, idempotent, block-scoped
------------------------------------------
* Touches ONLY the ``variable "custom_modules_ref"`` block. The prod
  ``variables.tf`` also holds ``odoo_image_tag`` / ``hub_image_tag`` /
  ``tenant_image_tag`` (a different repo's image pins); an unanchored
  ``s/default = "40hex"/.../`` would silently rewrite the wrong one
  (the GOL-1708 finding promote-storefronts.yml guards against). We locate the
  block by its ``variable "..." {`` header and its column-0 ``}`` and edit only
  inside it.
* Validates the new ref is a 40-char lowercase hex SHA — the SAME shape
  ``var.custom_modules_ref``'s own ``validation`` block enforces (GOL-892), so a
  branch name like ``main``/``HEAD`` can never be committed as the prod pin.
* Idempotent: if the default already equals the new ref, it makes NO change (and
  appends NO note) and reports ``nochange`` — the workflow then skips opening a
  PR. Re-running is always safe.

This is a DRIFT reconcile, never a deploy: it edits committed HCL only. Nothing
here applies terraform or touches the droplet.
"""

from __future__ import annotations

import argparse
import re
import sys

# Absolute-in-repo default so the workflow can call it with no args.
DEFAULT_TF_FILE = "infra/terraform/environments/production/variables.tf"
VAR_NAME = "custom_modules_ref"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ReconcileError(RuntimeError):
    """A fatal, human-actionable problem — printed and exits non-zero."""


def _find_block(text: str, var_name: str) -> tuple[int, int]:
    """Return (start, end) char offsets of the ``variable "<var_name>" { ... }``
    block, end being just past its column-0 closing brace. Raises if the block
    is missing or appears more than once."""
    header = re.compile(
        r'^variable\s+"' + re.escape(var_name) + r'"\s*\{', re.MULTILINE
    )
    matches = list(header.finditer(text))
    if not matches:
        raise ReconcileError(
            f'no `variable "{var_name}" {{` block found — wrong file or a rename?'
        )
    if len(matches) > 1:
        raise ReconcileError(
            f'expected exactly one `variable "{var_name}"` block, found {len(matches)}'
        )
    start = matches[0].start()
    # The block closes at the first line that is a lone `}` in column 0.
    closer = re.compile(r"^\}", re.MULTILINE)
    close_match = closer.search(text, matches[0].end())
    if not close_match:
        raise ReconcileError(
            f'`variable "{var_name}"` block is not closed by a column-0 `}}`'
        )
    return start, close_match.end()


def _current_default(block: str) -> str:
    m = re.search(r'^\s*default\s*=\s*"([0-9a-f]{40})"\s*$', block, re.MULTILINE)
    if not m:
        raise ReconcileError(
            "could not read the current 40-hex `default` in the block "
            "(is it already a non-SHA / multi-line value?)"
        )
    return m.group(1)


def reconcile(text: str, new_ref: str, note: str) -> tuple[str, str]:
    """Return (new_text, status). status is 'changed' or 'nochange'.

    On 'nochange' the returned text is byte-identical to the input (no note is
    appended when the value did not move)."""
    if not SHA_RE.match(new_ref):
        raise ReconcileError(
            f"'{new_ref}' is not a 40-char lowercase hex commit SHA — "
            "prod never tracks a moving ref (GOL-892)."
        )
    if '"' in note:
        # The note is spliced into a double-quoted HCL string; a stray quote
        # would terminate it early and produce invalid HCL.
        raise ReconcileError('note must not contain a double-quote character (")')
    note = note.replace("\n", " ").strip()

    start, end = _find_block(text, VAR_NAME)
    block = text[start:end]

    old_ref = _current_default(block)
    if old_ref == new_ref:
        return text, "nochange"

    # 1) Swap the default value (anchored inside this block only).
    new_block, n = re.subn(
        r'(^\s*default\s*=\s*")[0-9a-f]{40}("\s*$)',
        r"\g<1>" + new_ref + r"\g<2>",
        block,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise ReconcileError("failed to rewrite the `default` line (unexpected shape)")

    # 2) Append the note to the description string, inserted before its closing
    #    quote. The description is a single physical line; the greedy `.*`
    #    captures through to the last quote on it.
    def _append_note(m: re.Match) -> str:
        return m.group(1) + " " + note + m.group(2)

    new_block, dn = re.subn(
        r'(^\s*description\s*=\s*".*?)("\s*)$',
        _append_note,
        new_block,
        count=1,
        flags=re.MULTILINE,
    )
    if dn != 1:
        raise ReconcileError("failed to locate the `description` line to annotate")

    return text[:start] + new_block + text[end:], "changed"


def _build_note(new_ref: str, date: str, extra: str) -> str:
    base = (
        f"RECONCILE {date}: converge the committed default onto the live "
        f"git-sync pin {new_ref[:12]} (drift-only, no roll-forward)."
    )
    extra = extra.strip()
    return f"{base} {extra}" if extra else base


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sha", required=True, help="new 40-hex grove-odoo-modules SHA")
    ap.add_argument(
        "--date",
        required=True,
        help="ISO date for the appended note (the workflow passes `date -u +%Y-%m-%d`)",
    )
    ap.add_argument(
        "--note",
        default="",
        help="optional extra context appended after the standard reconcile sentence",
    )
    ap.add_argument("--file", default=DEFAULT_TF_FILE, help="path to variables.tf")
    args = ap.parse_args(argv)

    try:
        with open(args.file, "r", encoding="utf-8") as fh:
            text = fh.read()
        note = _build_note(args.sha, args.date, args.note)
        new_text, status = reconcile(text, args.sha, note)
    except ReconcileError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError:
        print(f"::error::{args.file} not found", file=sys.stderr)
        return 2

    if status == "nochange":
        old = _current_default(text[slice(*_find_block(text, VAR_NAME))])
        print(f"nochange: default already at {old}")
        return 0

    with open(args.file, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    old = _current_default(text[slice(*_find_block(text, VAR_NAME))])
    print(f"changed: {old} -> {args.sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
