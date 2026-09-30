#!/usr/bin/env python3
"""Offline tests for verify-shop-departments-compat's comparison logic.

No network: they drive ``compare()`` with a hand-built "before" (today's live
prod shape, read 2026-09-30) and an "after" that simulates what
grove-odoo-modules#299 does to it. The point is to prove the gate FAILS on the
things it is supposed to catch — a gate that only ever passes is not a gate.

    python3 scripts/test_verify_shop_departments_compat.py
"""

import copy
import importlib.util
import pathlib
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "verify_shop_departments_compat",
    pathlib.Path(__file__).with_name("verify-shop-departments-compat.py"),
)
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)


def _category(cat_id, slug, name, product_ids):
    return {
        "id": cat_id,
        "slug": slug,
        "name": name,
        "product_ids": sorted(product_ids),
        "product_count": len(product_ids),
    }


# Today's live prod nursery catalog (ids/slugs/counts read off
# odoo.gatheringatthegrove.com on 2026-09-30). Category 5 "Fruiting Vines" has
# no published product, so it is absent from the derived inventory and only
# shows up in cat_counts — that asymmetry is intentional and tested below.
BEFORE = {
    "base_url": "https://odoo.example.invalid",
    "tenant": "nursery",
    "published_products": 19,
    "categories": {
        "1": _category(1, "fruit-trees", "Fruit Trees", range(101, 111)),
        "2": _category(2, "native", "Native", range(201, 206)),
        "3": _category(3, "nut-trees", "Nut Trees", [301]),
        "4": _category(4, "berry-nut-shrubs", "Berry & Nut Shrubs", [401]),
        "6": _category(6, "food-forest-packages", "Food Forest Packages", range(601, 606)),
    },
    "cat_counts": {
        "berry-nut-shrubs": 1,
        "food-forest-packages": 5,
        "fruit-trees": 10,
        "fruiting-vines": 0,
        "native": 5,
        "nut-trees": 1,
    },
}

GUILDS_RENAME = {"food-forest-packages": "guilds"}


def after_pr299():
    """What #299's 19.0.1.56.0 migration leaves behind, per its own code path.

    ids 1-5 keep slugify(name) as grove_slug and are reparented under the new
    "Orchard & food forest" root (reparenting is invisible here because ?cat=
    is exact-slug, non-recursive); id 6 is renamed AND re-slugged to Guilds;
    id 7 becomes the Mycoforestry department; new coming-soon children appear
    with no published products, so they never enter the derived inventory.
    """
    after = copy.deepcopy(BEFORE)
    after["categories"]["6"]["slug"] = "guilds"
    after["categories"]["6"]["name"] = "Guilds"
    after["cat_counts"]["guilds"] = 5
    after["cat_counts"]["food-forest-packages"] = 0
    return after


class CompareTests(unittest.TestCase):
    def test_no_change_is_compatible(self):
        self.assertEqual(gate.compare(BEFORE, copy.deepcopy(BEFORE), {}), [])

    def test_pr299_guilds_rename_is_flagged_when_not_declared(self):
        problems = gate.compare(BEFORE, after_pr299(), {})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("food-forest-packages", problems[0])
        self.assertIn("guilds", problems[0])

    def test_pr299_is_compatible_once_the_rename_is_declared(self):
        self.assertEqual(gate.compare(BEFORE, after_pr299(), GUILDS_RENAME), [])

    def test_declaring_a_rename_does_not_excuse_a_different_one(self):
        after = after_pr299()
        after["categories"]["1"]["slug"] = "orchard-fruit-trees"
        problems = gate.compare(BEFORE, after, GUILDS_RENAME)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("orchard-fruit-trees", problems[0])

    def test_a_vanished_category_is_flagged(self):
        after = after_pr299()
        del after["categories"]["3"]
        problems = gate.compare(BEFORE, after, GUILDS_RENAME)
        self.assertTrue(any("is GONE" in p for p in problems), problems)

    def test_a_changed_product_count_is_flagged(self):
        after = after_pr299()
        after["categories"]["1"]["product_ids"] = list(range(101, 108))
        after["categories"]["1"]["product_count"] = 7
        problems = gate.compare(BEFORE, after, GUILDS_RENAME)
        self.assertTrue(any("product count 10 -> 7" in p for p in problems), problems)

    def test_a_swapped_product_at_the_same_count_is_flagged(self):
        after = after_pr299()
        after["categories"]["2"]["product_ids"] = [201, 202, 203, 204, 999]
        problems = gate.compare(BEFORE, after, GUILDS_RENAME)
        self.assertTrue(any("different product set" in p for p in problems), problems)

    def test_a_storefront_pill_losing_its_products_is_flagged(self):
        """The GOL-760 bug class: the pill still renders, it just matches nothing."""
        after = after_pr299()
        after["cat_counts"]["native"] = 0
        problems = gate.compare(BEFORE, after, GUILDS_RENAME)
        self.assertTrue(any("storefront pill 'native'" in p for p in problems), problems)

    def test_an_empty_storefront_pill_stays_empty_without_complaint(self):
        """fruiting-vines has 0 published products today; 0 after is not a regression."""
        self.assertEqual(after_pr299()["cat_counts"]["fruiting-vines"], 0)
        self.assertEqual(gate.compare(BEFORE, after_pr299(), GUILDS_RENAME), [])

    def test_new_coming_soon_categories_do_not_trip_the_gate(self):
        after = after_pr299()
        after["categories"]["12"] = _category(12, "truffle-trees", "Truffle trees", [])
        self.assertEqual(gate.compare(BEFORE, after, GUILDS_RENAME), [])


class ParseAllowedTests(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(gate._parse_allowed(["a=b", " c = d "]), {"a": "b", "c": "d"})

    def test_rejects_a_pair_without_an_equals(self):
        with self.assertRaises(SystemExit):
            gate._parse_allowed(["food-forest-packages"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
