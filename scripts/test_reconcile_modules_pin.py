#!/usr/bin/env python3
"""Regression tests for scripts/reconcile_modules_pin.py (GOL-2281).

No network, no terraform. These exercise the pure edit function against a
minimal in-memory variables.tf fixture and against the REAL committed prod
variables.tf, asserting the properties that would actually cause a bad prod pin:

  swaps-only-the-target-block   the custom_modules_ref default moves; the
                                odoo_image_tag / hub_image_tag / tenant_image_tag
                                defaults in the same file are untouched (the
                                GOL-1708 wrong-block-rewrite trap).
  appends-note-in-description   the reconcile note lands inside the description
                                string, before its closing quote, so HCL stays
                                valid and the running log grows by one line.
  rejects-non-sha               branch names / short hashes are refused, mirror-
                                ing var.custom_modules_ref's own validation
                                (GOL-892) so prod can never track a moving ref.
  idempotent-nochange           re-running with the current default is a no-op
                                and appends NO note (so the workflow skips the PR).
  real-file-roundtrips          the actual committed variables.tf reconciles
                                cleanly and the result still validates.

    python3 scripts/test_reconcile_modules_pin.py
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)

import reconcile_modules_pin as rp  # noqa: E402

REAL_TF = os.path.join(
    _ROOT, "infra", "terraform", "environments", "production", "variables.tf"
)

OLD = "098422d28fc99da979e75b1201e88c573cabef10"
NEW = "22fbb71ead189c4da627528b8b86b5ced6fdaefa"

# A compact stand-in with the same shape as the real file: several 40-hex
# defaults, only one of which is custom_modules_ref.
FIXTURE = f'''\
variable "odoo_image_tag" {{
  description = "grove-odoo image tag."
  type        = string
  default     = "latest"
}}

variable "custom_modules_ref" {{
  description = "grove-odoo-modules pin. History: bumped to {OLD[:8]} for launch."
  type        = string
  default     = "{OLD}"

  validation {{
    condition     = can(regex("^[0-9a-f]{{40}}$", var.custom_modules_ref))
    error_message = "must be a 40-char hex SHA."
  }}
}}

variable "hub_image_tag" {{
  description = "hub image tag."
  type        = string
  default     = "{OLD}"
}}
'''

NOTE = rp._build_note(NEW, "2026-09-14", "")


def test_swaps_only_the_target_block():
    out, status = rp.reconcile(FIXTURE, NEW, NOTE)
    assert status == "changed", status
    # custom_modules_ref moved to NEW...
    start, end = rp._find_block(out, "custom_modules_ref")
    assert rp._current_default(out[start:end]) == NEW
    # ...but hub_image_tag's identical OLD default did NOT (wrong-block trap).
    hstart, hend = rp._find_block(out, "hub_image_tag")
    assert rp._current_default(out[hstart:hend]) == OLD
    # odoo_image_tag ("latest") is untouched and file still has exactly one NEW.
    assert 'default     = "latest"' in out
    assert out.count(NEW) == 1


def test_appends_note_in_description():
    out, _ = rp.reconcile(FIXTURE, NEW, NOTE)
    start, end = rp._find_block(out, "custom_modules_ref")
    block = out[start:end]
    desc_line = next(l for l in block.splitlines() if "description" in l)
    assert NOTE in desc_line, "note not on the description line"
    # Note sits INSIDE the quoted string (before the closing quote), so the
    # line still ends with `"` and HCL stays valid.
    assert desc_line.rstrip().endswith('"')
    assert desc_line.count('"') == 2
    # The original description text is preserved (append, not replace).
    assert "History: bumped to" in desc_line


def test_rejects_non_sha():
    for bad in ["main", "HEAD", NEW[:12], NEW.upper(), NEW + "00", ""]:
        try:
            rp.reconcile(FIXTURE, bad, NOTE)
        except rp.ReconcileError:
            continue
        raise AssertionError(f"non-SHA ref '{bad}' was not rejected")


def test_rejects_note_with_quote():
    try:
        rp.reconcile(FIXTURE, NEW, 'has a " quote')
    except rp.ReconcileError:
        return
    raise AssertionError("note containing a double-quote was not rejected")


def test_idempotent_nochange():
    out, status = rp.reconcile(FIXTURE, OLD, NOTE)
    assert status == "nochange", status
    assert out == FIXTURE, "nochange must not mutate the text (no note appended)"


def test_build_note_shape():
    note = rp._build_note(NEW, "2026-09-14", "prod incident GOL-2134 hand-edit.")
    assert note.startswith("RECONCILE 2026-09-14:")
    assert "no roll-forward" in note
    assert NEW[:12] in note
    assert note.endswith("prod incident GOL-2134 hand-edit.")


def test_real_file_roundtrips():
    with open(REAL_TF, "r", encoding="utf-8") as fh:
        text = fh.read()
    # The committed default today (reconciled by #650). If a future bump moves
    # it, this test reads whatever is current and reconciles to a distinct SHA.
    cur = rp._current_default(text[slice(*rp._find_block(text, "custom_modules_ref"))])
    target = NEW if cur != NEW else OLD
    out, status = rp.reconcile(text, target, rp._build_note(target, "2026-09-14", ""))
    assert status == "changed"
    # Exactly one block, default now the target, and the description grew by the
    # note (still one line, still balanced quotes).
    s, e = rp._find_block(out, "custom_modules_ref")
    block = out[s:e]
    assert rp._current_default(block) == target
    desc_line = next(l for l in block.splitlines() if l.lstrip().startswith("description"))
    assert desc_line.count('"') == 2
    assert "no roll-forward" in desc_line
    # Untargeted image-tag defaults are unchanged.
    assert rp._current_default(out[slice(*rp._find_block(out, "hub_image_tag"))]) == \
        rp._current_default(text[slice(*rp._find_block(text, "hub_image_tag"))])


def _run():
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
