#!/usr/bin/env python3
"""Delete the superseded hand-built prod ir.filters that grove_headless now owns.

GOL-3130 (follow-up of GOL-3056 / GOL-3114).

WHY THIS EXISTS
---------------
`grove_headless/data/grove_zone_filters.xml` (shipped in 19.0.1.66.0) declares
three global saved filters on `sale.order` as xml-id data records. Josh had
already hand-built the same three on prod on 2026-10-05 as `ir.filters` ids
9, 10 and 11. Those hand-made rows carry **no `ir.model.data` row**, so the
module's records are brand-new rows rather than updates -- after the Train #3
promote prod shows each filter twice.

Odoo 19's `ir.filters` has NO uniqueness constraint on (name, model_id, user)
-- verified against prod 2026-10-06, `ir.model.constraint` for the model is one
non-unique INDEX, two CHECKs and five FKs -- so the promote genuinely
duplicates instead of failing. That is what makes this a cleanup and not a
blocker on the promote itself.

THE SEQUENCING GUARD (the reason this is a script and not a hand-run unlink)
---------------------------------------------------------------------------
Running the delete BEFORE the promote leaves prod with NO balance-not-charged
filters at all until 19.0.1.66.0 is live. The ordering lives in a ticket
comment today, which is exactly the kind of success condition that gets held in
someone's head and then lost. So this script refuses to delete anything until
it has itself confirmed, against the live database:

  1. `grove_headless` installed_version >= 19.0.1.66.0            (exit 4)
  2. all three xml-id rows exist under module `grove_headless`
     and resolve to live `ir.filters` records                     (exit 5)
  3. every deletion candidate has NO `ir.model.data` row          (exit 6)
  4. candidate ids are a subset of the expected legacy ids        (exit 7)

It is dry-run by default and idempotent: with the duplicates already gone it
reports "already converged" and exits 0, so it is safe to re-run and safe to
run speculatively before the promote (it will just tell you to wait).

USAGE
-----
    # read-only: report what WOULD be deleted, touch nothing
    python3 scripts/prod_dedupe_ir_filters.py

    # actually delete (the literal token is required)
    CONFIRM=DELETE python3 scripts/prod_dedupe_ir_filters.py

Credentials come from the environment, or from 1Password when `op` is
available -- never from this file:

    ODOO_PROD_URL       default https://odoo.gatheringatthegrove.com
    ODOO_PROD_DB        default odoo
    ODOO_PROD_LOGIN     else op://Grove Prod/<item>/login
    ODOO_PROD_PASSWORD  else op://Grove Prod/<item>/confirm_password

⚠️ Writes over this login are attributed to Joshua Dunbar in prod chatter
(it is Josh's own account, uid 8). `ir.filters` rows are saved searches and
carry no chatter, so this particular delete leaves no mis-attributed trail --
but say so in the ticket anyway. Rollback is to re-create the three filters by
hand; no business data is involved.

Exit codes
----------
    0  converged (or dry-run completed)
    2  bad usage / missing credentials
    3  authentication failed
    4  grove_headless is older than the required version -- DO NOT DELETE
    5  the module's xml-id filters are missing
    6  a deletion candidate owns an xml-id (refusing to delete module data)
    7  a deletion candidate is outside the expected legacy id set
    8  delete ran but the post-state assertion failed
"""

from __future__ import annotations

import os
import subprocess
import sys
import xmlrpc.client

# --- what this script knows about ------------------------------------------

#: grove_headless version that first ships grove_zone_filters.xml (GOL-3056).
MIN_MODULE_VERSION = "19.0.1.66.0"

MODULE = "grove_headless"
MODEL = "sale.order"

#: xml-id name -> the filter `name` field it carries. Both are asserted; a
#: rename on either side must fail loudly rather than silently delete the wrong
#: row.
EXPECTED_FILTERS = {
    "ir_filter_grove_balance_not_charged": "Deposit orders: balance not charged",
    "ir_filter_grove_preorders_pickup": "Preorders for pickup (balance not charged)",
    "ir_filter_grove_preorders_shipping": "Preorders for shipping (balance not charged)",
}

#: The hand-built rows Josh created on prod 2026-10-05. Used as a CEILING on
#: what may be deleted, not as the lookup -- the lookup is by name + absent
#: xml-id so the script stays correct if ids ever differ.
EXPECTED_LEGACY_IDS = {9, 10, 11}

OP_ITEM = "op://Grove Prod/ajohqpb6niu2qw36ykzxqewxcu"

CONFIRM_TOKEN = "DELETE"


class Refused(Exception):
    """A fail-closed gate rejected the run. Carries the process exit code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- version comparison -----------------------------------------------------


def parse_version(raw: str) -> tuple[int, ...]:
    """`"19.0.1.66.0"` -> `(19, 0, 1, 66, 0)`.

    Non-numeric trailing segments are dropped rather than crashing: Odoo
    occasionally carries suffixes we do not want to reason about, and a version
    we cannot parse must not read as "newer".
    """
    parts: list[int] = []
    for seg in str(raw).strip().split("."):
        if not seg.isdigit():
            break
        parts.append(int(seg))
    return tuple(parts)


def version_at_least(installed: str, required: str) -> bool:
    """Zero-pad both to the same length, then compare as tuples.

    Padding rather than truncating to the shorter side: truncating makes a bare
    `"19.0"` compare equal to `"19.0.1.66.0"` and read as satisfied, which is
    the one direction this gate must never get wrong.
    """
    got, want = parse_version(installed), parse_version(required)
    if not got:
        return False
    width = max(len(got), len(want))

    def pad(v: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(v) + (0,) * (width - len(v))

    return pad(got) >= pad(want)


# --- the pure decision, over an injected client -----------------------------


def plan_deletion(client) -> dict:
    """Decide which `ir.filters` ids to delete. Reads only; raises `Refused`.

    `client` needs one method, `call(model, method, args=None, kwargs=None)`,
    so the whole gate chain is testable without a database.
    """
    mods = client.call(
        "ir.module.module",
        "search_read",
        [[["name", "=", MODULE]]],
        {"fields": ["name", "state", "installed_version"]},
    )
    if not mods:
        raise Refused(4, f"{MODULE} is not present on this database")
    mod = mods[0]
    installed = mod.get("installed_version") or ""
    if mod.get("state") != "installed":
        raise Refused(4, f"{MODULE} state is {mod.get('state')!r}, not 'installed'")
    if not version_at_least(installed, MIN_MODULE_VERSION):
        raise Refused(
            4,
            f"{MODULE} is {installed}, need >= {MIN_MODULE_VERSION}. The Train #3 "
            "promote has NOT landed, so the module's xml-id filters do not exist "
            "yet. Deleting the hand-built rows now would leave prod with NO "
            "balance-not-charged filters at all. Refusing.",
        )

    # The module's own rows, via ir.model.data.
    data = client.call(
        "ir.model.data",
        "search_read",
        [[["model", "=", "ir.filters"], ["module", "=", MODULE]]],
        {"fields": ["module", "name", "res_id"]},
    )
    owned = {d["name"]: d["res_id"] for d in data if d["name"] in EXPECTED_FILTERS}
    missing = sorted(set(EXPECTED_FILTERS) - set(owned))
    if missing:
        raise Refused(
            5,
            f"{MODULE} {installed} is installed but these xml-ids are missing: "
            f"{', '.join(missing)}. Expected them to be created by the promote; "
            "not deleting anything until the module's own rows are present.",
        )

    live = client.call(
        "ir.filters",
        "search_read",
        [[["model_id", "=", MODEL]]],
        {"fields": ["name", "model_id", "user_ids", "active"], "order": "id"},
    )
    live_by_id = {f["id"]: f for f in live}

    dangling = sorted(i for i in owned.values() if i not in live_by_id)
    if dangling:
        raise Refused(
            5,
            f"xml-id rows point at ir.filters ids {dangling} which are not live "
            f"on {MODEL} -- severed xml-ids, not a duplicate cleanup. Refusing.",
        )

    # Candidates: same name as a shipped filter, but NOT the shipped row.
    keep = set(owned.values())
    candidates = sorted(
        f["id"]
        for f in live
        if f["name"] in EXPECTED_FILTERS.values() and f["id"] not in keep
    )

    if candidates:
        # Belt and braces: never unlink anything that is module data, even if
        # some other module adopted it.
        adopted = client.call(
            "ir.model.data",
            "search_read",
            [[["model", "=", "ir.filters"], ["res_id", "in", candidates]]],
            {"fields": ["module", "name", "res_id"]},
        )
        if adopted:
            owners = ", ".join(f"{a['module']}.{a['name']} (id {a['res_id']})" for a in adopted)
            raise Refused(
                6,
                f"deletion candidate(s) own an xml-id: {owners}. These are module "
                "data, not Josh's hand-built rows. Refusing.",
            )

        unexpected = sorted(set(candidates) - EXPECTED_LEGACY_IDS)
        if unexpected:
            raise Refused(
                7,
                f"deletion candidate ids {unexpected} are outside the expected "
                f"legacy set {sorted(EXPECTED_LEGACY_IDS)}. Someone created more "
                "same-named filters by hand; re-triage before deleting.",
            )

    return {
        "installed_version": installed,
        "keep": {k: owned[k] for k in sorted(owned)},
        "delete": candidates,
        "live_by_id": live_by_id,
    }


def assert_converged(client) -> dict:
    """The issue's success condition, read back from the database.

    Exactly one `ir.filters` row per shipped name, and each survivor has an
    `ir.model.data` entry under `grove_headless`.
    """
    live = client.call(
        "ir.filters",
        "search_read",
        [[["model_id", "=", MODEL], ["name", "in", sorted(EXPECTED_FILTERS.values())]]],
        {"fields": ["name"], "order": "id"},
    )
    by_name: dict[str, list[int]] = {}
    for f in live:
        by_name.setdefault(f["name"], []).append(f["id"])

    problems = []
    for name in sorted(EXPECTED_FILTERS.values()):
        ids = by_name.get(name, [])
        if len(ids) != 1:
            problems.append(f"{name!r}: {len(ids)} rows {ids}, expected exactly 1")

    survivors = [i for ids in by_name.values() for i in ids]
    data = client.call(
        "ir.model.data",
        "search_read",
        [[["model", "=", "ir.filters"], ["res_id", "in", survivors]]],
        {"fields": ["module", "name", "res_id"]},
    )
    stamped = {d["res_id"] for d in data if d["module"] == MODULE}
    for name, ids in sorted(by_name.items()):
        for i in ids:
            if i not in stamped:
                problems.append(f"{name!r} id {i} has no {MODULE} ir.model.data entry")

    return {"by_name": by_name, "problems": problems}


# --- real transport ---------------------------------------------------------


def _op_read(field: str) -> str | None:
    try:
        out = subprocess.run(
            ["op", "read", f"{OP_ITEM}/{field}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


class OdooClient:
    """Minimal XML-RPC client. `read` needs `fields` as a kwarg, not a dict."""

    def __init__(self, url: str, db: str, login: str, password: str) -> None:
        self.url, self.db, self.password = url, db, password
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        self.uid = common.authenticate(db, login, password, {})
        if not self.uid:
            raise Refused(3, f"authentication failed for {login} on {url} db={db}")
        self.models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)

    def call(self, model: str, method: str, args=None, kwargs=None):
        return self.models.execute_kw(
            self.db, self.uid, self.password, model, method, list(args or []), kwargs or {}
        )


def connect() -> OdooClient:
    url = os.environ.get("ODOO_PROD_URL", "https://odoo.gatheringatthegrove.com")
    db = os.environ.get("ODOO_PROD_DB", "odoo")
    login = os.environ.get("ODOO_PROD_LOGIN") or _op_read("login")
    password = os.environ.get("ODOO_PROD_PASSWORD") or _op_read("confirm_password")
    if not login or not password:
        raise Refused(
            2,
            "no credentials: set ODOO_PROD_LOGIN + ODOO_PROD_PASSWORD, or make "
            f"`op` available for {OP_ITEM}",
        )
    return OdooClient(url, db, login, password)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print(__doc__)
        return 2

    confirm = os.environ.get("CONFIRM", "")
    dry_run = confirm != CONFIRM_TOKEN
    if confirm and dry_run:
        print(f"CONFIRM={confirm!r} is not the literal {CONFIRM_TOKEN!r} -- dry run.")

    try:
        client = connect()
        print(f"connected uid={client.uid} db={client.db} url={client.url}")
        plan = plan_deletion(client)
    except Refused as exc:
        print(f"REFUSED (exit {exc.code}): {exc}", file=sys.stderr)
        return exc.code

    print(f"{MODULE} installed_version = {plan['installed_version']}")
    print("keep (module xml-id rows):")
    for xmlid, rid in plan["keep"].items():
        print(f"  id {rid:<4} {MODULE}.{xmlid}")

    if not plan["delete"]:
        state = assert_converged(client)
        if state["problems"]:
            for p in state["problems"]:
                print(f"  ! {p}", file=sys.stderr)
            print("no duplicates to delete, but the success condition is NOT met.", file=sys.stderr)
            return 8
        print("\nalready converged: exactly one row per filter name, all xml-id stamped.")
        return 0

    print("delete (hand-built, no xml-id):")
    for rid in plan["delete"]:
        print(f"  id {rid:<4} {plan['live_by_id'][rid]['name']!r}")

    if dry_run:
        print(f"\nDRY RUN -- nothing deleted. Re-run with CONFIRM={CONFIRM_TOKEN} to apply.")
        return 0

    client.call("ir.filters", "unlink", [plan["delete"]])
    print(f"\nunlinked ir.filters {plan['delete']}")

    state = assert_converged(client)
    if state["problems"]:
        for p in state["problems"]:
            print(f"  ! {p}", file=sys.stderr)
        return 8
    for name, ids in sorted(state["by_name"].items()):
        print(f"  ok  {name!r} -> id {ids[0]}")
    print("\nconverged: exactly one row per filter name, all xml-id stamped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
