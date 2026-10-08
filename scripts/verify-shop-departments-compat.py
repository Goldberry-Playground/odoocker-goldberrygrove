#!/usr/bin/env python3
"""
verify-shop-departments-compat — prove the shop-departments migration
(grove-odoo-modules#299, GOL-2744, grove_headless 19.0.1.56.0) did not change
what a shopper sees.

WHY THIS EXISTS
---------------
#299 restructures ``product.public.category`` in place: it backfills
``grove_slug``, renames one category, reparents the orchard categories under a
new "Orchard & food forest" department root and creates coming-soon children.
The storefront shipping in Train #2 still browses with the FIVE HARDCODED slugs
in ``grove-sites/apps/nursery/data/categories.ts``:

    native · fruit-trees · nut-trees · berry-nut-shrubs · fruiting-vines

Those slugs are matched against ``product.categories[].slug`` in the API
payload, which after #299 is ``grove_slug or slugify(name)``. So the whole
backward-compatibility question reduces to a single, checkable claim:

    every category a shopper can reach today must still be reachable,
    under the same slug, with the same product set, after the upgrade.

This script checks that claim against the LIVE PUBLIC API — the same surface
the storefront calls — so a green run means the storefront works, not that the
ORM looks right.

NO CREDENTIALS
--------------
Everything is read from ``GET /grove/api/v1/products``, which is
``auth="public"``. No Odoo login, no 1Password, no SSH. That is deliberate:
the gate has to be runnable from CI, from the agent plane and by hand, in the
middle of a release window, without anyone minting a credential.

The category inventory is derived from the products themselves — each product
serializes ``categories: [{id, name, slug}]`` — so the baseline needs no
privileged model read and automatically covers exactly the categories a
shopper can actually land on.

TENANT HEADER IS MANDATORY
--------------------------
``/grove/api/v1/*`` routes are ``website=True`` and resolve their company from
``X-Grove-Tenant`` (goldberry | ggg | nursery). Without it you silently get
company 1 and a correct answer to the wrong question. ``--tenant`` defaults to
``nursery`` and is always sent.

USAGE
-----
    # BEFORE `scripts/qa-module-upgrade.sh grove_headless`:
    python3 scripts/verify-shop-departments-compat.py snapshot \
        --base-url https://odoo.qa.gatheringatthegrove.com \
        --out /tmp/qa-cats-before.json

    # AFTER the upgrade:
    python3 scripts/verify-shop-departments-compat.py verify \
        --base-url https://odoo.qa.gatheringatthegrove.com \
        --baseline /tmp/qa-cats-before.json

An INTENTIONAL slug change is declared, never inferred:

    --allow-slug-change food-forest-packages=guilds

Anything not declared is a regression.

READING THE PILLS OFF THE LIVE STOREFRONT
-----------------------------------------
``STOREFRONT_SLUGS`` below is a COPY of a list that lives in another repo. A
copy drifts. ``--storefront-url`` removes the copy from the loop: the gate
GETs ``<storefront>/shop``, harvests the ``?cat=`` slugs that page actually
links, and uses those as the pill set.

    --storefront-url https://atthegrovenursery.com

That buys two checks the constant cannot give:

  * a slug change is only "declared away" for BOOKMARKS. If the live
    storefront still LINKS the old slug, the declaration does not cover it and
    the run fails — a navigable dead pill is never acceptable.
  * if the deployed storefront's pill set moves between the before and after
    snapshots, the run fails: the pinned build changed underneath the window
    and the whole before/after comparison is measuring two different shops.

Read off prod 2026-09-30, ``/shop`` links exactly five slugs — native,
fruit-trees, nut-trees, berry-nut-shrubs, fruiting-vines — and NOT
food-forest-packages, which is why #299's rename is declarable at all.

EXIT CODES
----------
    0  compatible (or snapshot written)
    1  usage / transport / parse failure
    2  REGRESSION — a category, slug, product count or PDP changed
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# The slugs the Train #2 storefront hardcodes. Sourced from
# grove-sites/apps/nursery/data/categories.ts (NURSERY_CATEGORIES). If one of
# these stops resolving, the pill silently matches nothing — the GOL-760 bug
# class. Checked even when a baseline does not contain it.
STOREFRONT_SLUGS = (
    "native",
    "fruit-trees",
    "nut-trees",
    "berry-nut-shrubs",
    "fruiting-vines",
)

PRODUCTS_PATH = "/grove/api/v1/products"
SHOP_PATH = "/shop"
USER_AGENT = "grove-shop-depts-compat/1.0"
PAGE_LIMIT = 200

# ``?cat=<slug>`` / ``&cat=<slug>`` as the storefront emits it — in plain
# anchors and, escaped, inside the RSC flight payload. Both forms match.
PILL_HREF_RE = re.compile(r"[?&]cat=([A-Za-z0-9][A-Za-z0-9._-]*)")


def _get_json(url: str, tenant: str, timeout: int):
    """GET a JSON document with the tenant header set. Raises on transport error."""
    request = urllib.request.Request(
        url,
        headers={
            "X-Grove-Tenant": tenant,
            # Cloudflare 403s the stdlib default User-Agent on the Grove
            # origins, in the same shape as an auth failure. Always name it.
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get_text(url: str, timeout: int) -> str:
    """GET a text document (the storefront's rendered HTML). Raises on transport error."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_storefront_pill_slugs(storefront_url: str, timeout: int) -> list:
    """The ``?cat=`` slugs the DEPLOYED storefront's /shop page actually links.

    Raises ``ValueError`` when the page links none. An empty pill set would
    make every pill assertion below vacuously true, so it is treated as a
    broken probe, never as "no pills to check".
    """
    html = _get_text(f"{storefront_url.rstrip('/')}{SHOP_PATH}", timeout)
    slugs = sorted(set(PILL_HREF_RE.findall(html)))
    if not slugs:
        raise ValueError(
            f"{storefront_url}{SHOP_PATH} linked no ?cat= slugs at all — the page "
            f"shape changed or it rendered empty; refusing to check nothing"
        )
    return slugs


def _results(payload) -> list:
    """The product array out of a /products response (shape: count/limit/offset/results)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("results") or []
    return []


def fetch_catalog(base_url: str, tenant: str, timeout: int) -> list:
    """Every published product for this tenant, following offset pagination."""
    products: list = []
    offset = 0
    while True:
        url = f"{base_url}{PRODUCTS_PATH}?limit={PAGE_LIMIT}&offset={offset}"
        _, payload = _get_json(url, tenant, timeout)
        page = _results(payload)
        products.extend(page)
        if len(page) < PAGE_LIMIT:
            return products
        offset += PAGE_LIMIT


def fetch_cat_count(base_url: str, tenant: str, slug: str, timeout: int) -> int:
    """How many products ``?cat=<slug>`` returns — the server-side filter's own answer.

    An unknown slug resolves to the ``[-1]`` guard, i.e. 0 — never the whole
    catalog — so 0 here means "this slug reaches nothing".
    """
    url = f"{base_url}{PRODUCTS_PATH}?cat={urllib.parse.quote(slug)}&limit={PAGE_LIMIT}"
    _, payload = _get_json(url, tenant, timeout)
    return len(_results(payload))


def build_snapshot(
    base_url: str,
    tenant: str,
    timeout: int,
    storefront_url: str = None,
    extra_slugs=(),
) -> dict:
    """Category inventory + per-slug counts + one PDP probe id per category.

    ``storefront_url`` swaps the hardcoded ``STOREFRONT_SLUGS`` for the pills
    the live shop page links. ``extra_slugs`` forces additional slugs into
    ``cat_counts`` — verify passes the baseline's pills so both snapshots
    always measure the same set, even if the storefront changed.
    """
    if storefront_url:
        pill_slugs = fetch_storefront_pill_slugs(storefront_url, timeout)
        pill_source = "live"
    else:
        pill_slugs = list(STOREFRONT_SLUGS)
        pill_source = "builtin"

    products = fetch_catalog(base_url, tenant, timeout)
    categories: dict[str, dict] = {}
    for product in products:
        for category in product.get("categories") or []:
            key = str(category.get("id"))
            entry = categories.setdefault(
                key,
                {
                    "id": category.get("id"),
                    "name": category.get("name"),
                    "slug": category.get("slug"),
                    "product_ids": [],
                },
            )
            entry["product_ids"].append(product.get("id"))

    slugs = {entry["slug"] for entry in categories.values() if entry["slug"]}
    slugs.update(pill_slugs)
    slugs.update(extra_slugs)
    cat_counts = {slug: fetch_cat_count(base_url, tenant, slug, timeout) for slug in sorted(slugs)}

    for entry in categories.values():
        entry["product_ids"] = sorted(set(entry["product_ids"]))
        entry["product_count"] = len(entry["product_ids"])

    return {
        "base_url": base_url,
        "tenant": tenant,
        "published_products": len(products),
        "categories": categories,
        "cat_counts": cat_counts,
        "storefront_url": storefront_url,
        "storefront_slugs": pill_slugs,
        "storefront_slugs_source": pill_source,
    }


def compare(before: dict, after: dict, allowed_slug_changes: dict[str, str]) -> list[str]:
    """Return a list of regression messages; empty means compatible."""
    problems: list[str] = []
    before_cats = before.get("categories", {})
    after_cats = after.get("categories", {})

    # 1. No category a shopper could reach may disappear. The migration only
    #    ever writes and creates; a missing id means something unlinked it.
    for key, old in sorted(before_cats.items(), key=lambda kv: int(kv[0])):
        new = after_cats.get(key)
        if new is None:
            problems.append(
                f"category id {old['id']} ({old['name']!r}, slug {old['slug']!r}) "
                f"is GONE from the catalog after the upgrade"
            )
            continue

        # 2. Slug stability — the ?cat= URL contract. A rename is fine; a slug
        #    change is a broken URL unless it was declared.
        expected = allowed_slug_changes.get(old["slug"], old["slug"])
        if new["slug"] == old["slug"] != expected:
            # A declaration is a promise, not a permission: the slug was
            # supposed to move and did not. Almost always means the upgrade
            # never ran — say that, rather than "changed 'x' -> 'x'".
            problems.append(
                f"category id {old['id']} slug is STILL {old['slug']!r} but a change to "
                f"{expected!r} was declared — the migration did not run"
            )
        elif new["slug"] != expected:
            problems.append(
                f"category id {old['id']} slug changed {old['slug']!r} -> {new['slug']!r} "
                f"(expected {expected!r}) — /shop?cat={old['slug']} no longer resolves"
            )

        # 3. Same product set. Reparenting must not move products between
        #    categories, and ?cat= is exact-slug (non-recursive) so counts hold.
        if new["product_count"] != old["product_count"]:
            problems.append(
                f"category id {old['id']} ({old['name']!r}) product count "
                f"{old['product_count']} -> {new['product_count']}"
            )
        elif new["product_ids"] != old["product_ids"]:
            problems.append(
                f"category id {old['id']} ({old['name']!r}) holds a different product set "
                f"(same size, different ids)"
            )

    # 4. Every pill the storefront offers must still return what it did.
    #    Checked against the server-side ?cat= filter, not the derived counts,
    #    because that is the second code path the storefront can take. The set
    #    is whatever the baseline recorded — the live /shop page when the
    #    snapshot was taken with --storefront-url, else the hardcoded copy.
    pill_slugs = list(before.get("storefront_slugs") or STOREFRONT_SLUGS)
    for slug in pill_slugs:
        old_count = before.get("cat_counts", {}).get(slug)
        new_count = after.get("cat_counts", {}).get(slug)
        if old_count is None or new_count is None:
            problems.append(f"storefront slug {slug!r} missing from a snapshot's cat_counts")
        elif old_count != new_count:
            problems.append(
                f"storefront pill {slug!r}: ?cat= returned {old_count} products before, "
                f"{new_count} after"
            )

    # 5. A declared slug change buys forgiveness for BOOKMARKS only. If the
    #    storefront still links the old slug, the declaration does not cover
    #    it: the pill renders, is clickable, and lands on nothing.
    for old_slug, new_slug in sorted(allowed_slug_changes.items()):
        if old_slug in pill_slugs:
            problems.append(
                f"declared slug change {old_slug!r} -> {new_slug!r} is NOT safe: the "
                f"storefront still links ?cat={old_slug} — declaring a rename covers "
                f"bookmarks, not a live pill"
            )

    # 6. Both snapshots must have been taken against the same deployed
    #    storefront. If the pinned build moved mid-window the before/after
    #    pair is comparing two different shops and proves nothing.
    if (
        before.get("storefront_slugs_source") == "live"
        and after.get("storefront_slugs_source") == "live"
        and sorted(before.get("storefront_slugs") or []) != sorted(after.get("storefront_slugs") or [])
    ):
        problems.append(
            f"the deployed storefront's pill set changed mid-run: "
            f"{sorted(before.get('storefront_slugs') or [])} -> "
            f"{sorted(after.get('storefront_slugs') or [])}"
        )

    return problems


def _probe_pdps(base_url: str, tenant: str, snapshot: dict, timeout: int) -> list[str]:
    """One PDP per category must still answer 200 with the same id (checkout entry point)."""
    problems: list[str] = []
    for entry in sorted(snapshot.get("categories", {}).values(), key=lambda e: e["id"]):
        if not entry["product_ids"]:
            continue
        product_id = entry["product_ids"][0]
        url = f"{base_url}{PRODUCTS_PATH}/{product_id}"
        try:
            status, payload = _get_json(url, tenant, timeout)
        except urllib.error.HTTPError as exc:
            problems.append(f"PDP {product_id} (category {entry['id']}) returned HTTP {exc.code}")
            continue
        if status != 200 or (payload or {}).get("id") != product_id:
            problems.append(f"PDP {product_id} (category {entry['id']}) did not echo its own id")
    return problems


def _parse_allowed(pairs: list[str]) -> dict[str, str]:
    allowed: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--allow-slug-change expects old=new, got {pair!r}")
        old, new = pair.split("=", 1)
        allowed[old.strip()] = new.strip()
    return allowed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("snapshot", "verify"))
    parser.add_argument("--base-url", required=True, help="e.g. https://odoo.qa.gatheringatthegrove.com")
    parser.add_argument("--tenant", default="nursery", choices=("nursery", "goldberry", "ggg"))
    parser.add_argument("--out", help="snapshot: where to write the baseline JSON")
    parser.add_argument("--baseline", help="verify: the snapshot written before the upgrade")
    parser.add_argument(
        "--allow-slug-change",
        action="append",
        default=[],
        metavar="OLD=NEW",
        help="declare an INTENTIONAL slug change, e.g. food-forest-packages=guilds",
    )
    parser.add_argument(
        "--storefront-url",
        help="read the pill slugs off this deployed storefront's /shop instead of "
             "the hardcoded copy, e.g. https://atthegrovenursery.com",
    )
    parser.add_argument("--skip-pdp", action="store_true", help="skip the per-category PDP probe")
    parser.add_argument("--timeout", type=int, default=40)
    parser.add_argument("--json", action="store_true", help="emit the machine-readable result on stdout")
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")

    # The baseline is loaded BEFORE the live read so the after-snapshot can
    # force the baseline's pill slugs into its own cat_counts. Otherwise a
    # storefront that changed mid-window would report the pills as "missing
    # from a snapshot" instead of as the drift they are.
    before = None
    if args.mode == "verify":
        if not args.baseline:
            print("ERROR: verify needs --baseline", file=sys.stderr)
            return 1
        try:
            with open(args.baseline, encoding="utf-8") as handle:
                before = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"ERROR: cannot read baseline {args.baseline}: {exc}", file=sys.stderr)
            return 1
    elif not args.out:
        print("ERROR: snapshot needs --out", file=sys.stderr)
        return 1

    try:
        snapshot = build_snapshot(
            base_url,
            args.tenant,
            args.timeout,
            storefront_url=args.storefront_url,
            extra_slugs=(before or {}).get("storefront_slugs") or (),
        )
    except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not build a snapshot from {base_url}: {exc}", file=sys.stderr)
        return 1

    if args.mode == "snapshot":
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
        print(
            f"baseline written to {args.out}: {len(snapshot['categories'])} categories, "
            f"{snapshot['published_products']} published products, "
            f"{len(snapshot['storefront_slugs'])} {snapshot['storefront_slugs_source']} pills",
            file=sys.stderr,
        )
        if args.json:
            print(json.dumps(snapshot, indent=2, sort_keys=True))
        return 0

    problems = compare(before, snapshot, _parse_allowed(args.allow_slug_change))
    if not args.skip_pdp:
        try:
            problems.extend(_probe_pdps(base_url, args.tenant, snapshot, args.timeout))
        except urllib.error.URLError as exc:
            print(f"ERROR: PDP probe transport failure: {exc}", file=sys.stderr)
            return 1

    result = {
        "compatible": not problems,
        "problems": problems,
        "categories_before": len(before.get("categories", {})),
        "categories_after": len(snapshot.get("categories", {})),
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))

    if problems:
        print(f"REGRESSION — {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2

    print(
        f"compatible: {result['categories_before']} pre-upgrade categories all still reachable "
        f"under their own slug with the same products",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
