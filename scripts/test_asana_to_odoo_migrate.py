#!/usr/bin/env python3
"""Unit tests for the pure transform layer of asana_to_odoo_migrate.py.

No network / no Odoo — exercises the CEO-ratified rules (dedupe, prune, company
scoping, comment folding, stage derivation, subtask carry, idempotency marker)
against a fixture that mirrors real Asana export shapes.

Run:  python3 scripts/test_asana_to_odoo_migrate.py
      (or: python3 -m pytest scripts/test_asana_to_odoo_migrate.py)
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    "a2o", os.path.join(_HERE, "asana_to_odoo_migrate.py"))
a2o = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(a2o)

TODAY = dt.date(2026, 9, 5)


def _task(gid, name, **kw):
    t = {"gid": gid, "name": name, "notes": kw.get("notes", ""),
         "completed": kw.get("completed", False),
         "assignee": kw.get("assignee"), "due_on": kw.get("due_on"),
         "parent": kw.get("parent"), "section_gid": kw.get("section_gid"),
         "memberships": kw.get("memberships", []),
         "custom_fields": kw.get("custom_fields", []),
         "stories": kw.get("stories", [])}
    return t


class ProjectScoping(unittest.TestCase):
    def test_dev_maps_company_1(self):
        t = a2o.project_target({"gid": "1213867393569940", "name": "Gather at the Grove | Dev"})
        self.assertEqual(t["action"], "migrate")
        self.assertEqual(t["company"], 1)

    def test_nursery_ops_maps_company_3(self):
        t = a2o.project_target({"gid": "1213898117964570", "name": "Ops"})
        self.assertEqual(t["company"], 3)

    def test_personal_excluded(self):
        t = a2o.project_target({"gid": "1213898907106904", "name": "Josh - Personal Tasks"})
        self.assertEqual(t["action"], "exclude")

    def test_unknown_project_defaults_co1_with_review(self):
        t = a2o.project_target({"gid": "999", "name": "New Project"})
        self.assertEqual(t["action"], "migrate")
        self.assertEqual(t["company"], 1)
        self.assertTrue(t["review"])

    def test_erp_features_flagged_for_review(self):
        t = a2o.project_target({"gid": "1213903263906001", "name": "ERP"})
        self.assertEqual(t["company"], 3)
        self.assertTrue(t["review"])


class Dedupe(unittest.TestCase):
    def test_gol_ref_in_name_is_twin(self):
        self.assertTrue(a2o.is_paperclip_twin(_task("1", "Fix checkout GOL-1880"), None))

    def test_gol_ref_in_notes_is_twin(self):
        self.assertTrue(a2o.is_paperclip_twin(_task("1", "Fix", notes="tracked in GOL-2094"), None))

    def test_gath_custom_ref_is_not_twin(self):
        self.assertFalse(a2o.is_paperclip_twin(_task("1", "Do thing GATH-132"), None))

    def test_qa_feedback_section_is_twin(self):
        self.assertTrue(a2o.is_paperclip_twin(_task("1", "Tester note"),
                                              "QA Round — Tester Feedback"))


class Prune(unittest.TestCase):
    def test_completed_skipped(self):
        self.assertEqual(a2o.prune_reason(_task("1", "x", completed=True), TODAY), "completed")

    def test_past_due_skipped(self):
        self.assertTrue(a2o.prune_reason(_task("1", "x", due_on="2026-08-07"), TODAY)
                        .startswith("past-due"))

    def test_future_due_kept(self):
        self.assertIsNone(a2o.prune_reason(_task("1", "x", due_on="2026-09-15"), TODAY))

    def test_due_today_kept(self):
        self.assertIsNone(a2o.prune_reason(_task("1", "x", due_on="2026-09-05"), TODAY))

    def test_undated_active_kept(self):
        self.assertIsNone(a2o.prune_reason(_task("1", "x", due_on=None), TODAY))

    def test_self_referential_skipped(self):
        self.assertEqual(a2o.prune_reason(_task("1", "Migrate this Asana board"), TODAY),
                         "self-referential migration task")


class CommentFolding(unittest.TestCase):
    def test_only_comments_kept(self):
        stories = [
            {"resource_subtype": "assigned", "text": "assigned to Josh",
             "created_by": {"name": "Sys"}, "created_at": "2026-08-01T10:00:00Z"},
            {"resource_subtype": "comment_added", "text": "Please prioritize.",
             "created_by": {"name": "Josh"}, "created_at": "2026-08-02T12:30:00Z"},
            {"resource_subtype": "comment_added", "text": "   ",
             "created_by": {"name": "Josh"}, "created_at": "2026-08-03T12:30:00Z"},
        ]
        kept = a2o.comment_stories(stories)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["text"], "Please prioritize.")

    def test_description_folds_author_time_and_marker(self):
        task = _task("111", "Do the thing", notes="Body text.",
                     assignee={"name": "Joshua Dunbar", "email": "josh@goldberrygrove.farm"},
                     due_on="2026-09-15",
                     custom_fields=[{"name": "GATH", "display_value": "GATH-9"}],
                     stories=[{"resource_subtype": "comment_added", "text": "note one",
                               "created_by": {"name": "Josh"},
                               "created_at": "2026-08-02T12:30:00Z"}])
        desc = a2o.fold_description(task, gid_field_present=False)
        self.assertIn("Body text.", desc)
        self.assertIn("Original Asana assignee: Joshua Dunbar", desc)
        self.assertIn("Original due date: 2026-09-15", desc)
        self.assertIn("GATH: GATH-9", desc)
        self.assertIn("[Josh · 2026-08-02 12:30] note one", desc)
        self.assertIn(f"{a2o.GID_MARKER_PREFIX} 111", desc)

    def test_no_marker_when_field_present(self):
        desc = a2o.fold_description(_task("111", "x"), gid_field_present=True)
        self.assertNotIn(a2o.GID_MARKER_PREFIX, desc)


class Stages(unittest.TestCase):
    def test_sections_become_stages(self):
        proj = {"sections": [{"gid": "s1", "name": "Backlog"}, {"gid": "s2", "name": "Doing"}]}
        self.assertEqual(a2o.stage_names_for_project(proj, [], {}), ["Backlog", "Doing"])

    def test_degenerate_gets_default_stages(self):
        self.assertEqual(a2o.stage_names_for_project({"sections": []}, [], {}),
                         a2o.DEFAULT_STAGES)

    def test_skip_section_excluded_from_stages(self):
        proj = {"sections": [{"gid": "s1", "name": "QA Round — Tester Feedback"},
                             {"gid": "s2", "name": "Backlog"}]}
        self.assertEqual(a2o.stage_names_for_project(proj, [], {}), ["Backlog"])


class Plan(unittest.TestCase):
    def _archive(self):
        return {
            "generated_at": "2026-09-05T00:00:00Z",
            "projects": [
                {   # company 1, mixed fates
                    "gid": "1213867393569940", "name": "Gather at the Grove | Dev",
                    "archived": False,
                    "sections": [{"gid": "s1", "name": "Backlog"},
                                 {"gid": "sqa", "name": "QA Round — Tester Feedback"}],
                    "tasks": [
                        _task("t1", "Future work", due_on="2026-09-15", section_gid="s1"),
                        _task("t2", "Past backlog", due_on="2026-08-01", section_gid="s1"),
                        _task("t3", "Done thing", completed=True, section_gid="s1"),
                        _task("t4", "Twin GOL-2094", section_gid="s1"),
                        _task("t5", "Tester feedback item", section_gid="sqa"),
                        _task("t6", "Undated active", section_gid="s1"),
                        _task("t6dup", "Undated active", section_gid="s1"),  # exact dup
                        _task("t7", "A subtask", parent={"gid": "t1"}, section_gid="s1"),
                    ],
                },
                {   # excluded
                    "gid": "1213898907106904", "name": "Josh - Personal Tasks",
                    "archived": False, "sections": [], "tasks": [_task("p1", "personal")],
                },
                {   # nursery co3, degenerate sections
                    "gid": "1213898117964570", "name": "Ops (Nursery & Orchard)",
                    "archived": False, "sections": [],
                    "tasks": [_task("n1", "Nursery active task")],
                },
            ],
        }

    def test_plan_counts_and_scoping(self):
        plan = a2o.build_plan(self._archive(), TODAY, gid_field_present=False)
        by_name = {p["asana_name"]: p for p in plan["projects"]}

        dev = by_name["Gather at the Grove | Dev"]
        self.assertEqual(dev["action"], "migrate")
        self.assertEqual(dev["company_id"], 1)
        # kept: t1 (future), t6 (undated), t7 (subtask) = 3
        kept_gids = {t["gid"] for t in dev["tasks"]}
        self.assertEqual(kept_gids, {"t1", "t6", "t7"})
        skip_reasons = {s["gid"]: s["reason"] for s in dev["skipped"]}
        self.assertIn("prune: past-due", skip_reasons["t2"])
        self.assertIn("completed", skip_reasons["t3"])
        self.assertIn("dedupe", skip_reasons["t4"])
        self.assertIn("dedupe", skip_reasons["t5"])
        self.assertIn("exact-duplicate", skip_reasons["t6dup"])
        # subtask carries its parent gid
        sub = next(t for t in dev["tasks"] if t["gid"] == "t7")
        self.assertEqual(sub["parent_gid"], "t1")

        self.assertNotIn("Josh - Personal Tasks", {p["asana_name"] for p in plan["projects"]
                                                   if p["action"] == "migrate"})
        nursery = by_name["Ops (Nursery & Orchard)"]
        self.assertEqual(nursery["company_id"], 3)
        self.assertEqual(nursery["stages"], a2o.DEFAULT_STAGES)

        self.assertEqual(plan["totals"]["projects_migrate"], 2)
        self.assertEqual(plan["totals"]["projects_excluded"], 1)
        self.assertEqual(plan["totals"]["tasks_migrate"], 4)   # 3 dev + 1 nursery
        self.assertEqual(plan["totals"]["tasks_subtasks"], 1)

    def test_archived_project_excluded(self):
        arch = self._archive()
        arch["projects"][0]["archived"] = True
        plan = a2o.build_plan(arch, TODAY, gid_field_present=False)
        dev = next(p for p in plan["projects"] if p["asana_name"].endswith("Dev"))
        self.assertEqual(dev["action"], "exclude")
        self.assertEqual(dev["reason"], "archived project")


if __name__ == "__main__":
    unittest.main(verbosity=2)
