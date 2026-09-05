#!/usr/bin/env python3
"""
WS3b — Asana -> Odoo project.task migration (GOL-2094, under epic GOL-2092).

Reads a frozen Asana workspace export (the WS3a archive — GOL-2093, the
rollback surface) and writes the surviving work into Odoo Projects via the
external XML-RPC API. Migrating from the archive rather than live Asana makes
the run deterministic and re-playable: the same input always produces the same
plan, and the archive is the single thing we can roll back to.

Read path is the WS3a JSON export (itself produced from the Asana MCP/API in
GOL-2093); write path is Odoo XML-RPC, matching the odoocker house style of
skills/odoo-logistics/scripts/odoo_client.py (stdlib-only, env-injected creds,
JSON on stdout / logs on stderr, non-zero exit on failure).

CEO-RATIFIED MAPPING RULES (2026-09-05, recorded on GOL-2094)
-------------------------------------------------------------
1. DEDUPE — skip Paperclip twins. A task that already lives as a GOL issue
   (a GOL-\\d+ reference in its title/notes, or the Dev project's
   "QA Round — Tester Feedback" section) is NOT imported. Paperclip is the
   agent-execution layer; Odoo Projects is the human layer. Skips are logged.
   NOTE: the "GATH-\\d+" custom field is an Asana-native ref, NOT a GOL twin —
   those tasks migrate normally.
2. PRUNE — migrate only living work: tasks that are due-in-future OR
   undated-and-active. Completed tasks, past-due backlog, exact-duplicate
   tasks, and the self-referential "Migrate this Asana board" task are left
   behind (WS3d closes them in Asana with a not-migrated tag; the WS3a archive
   preserves everything).
3. COMPANY SCOPING — company 1 (Goldberry Grove Farm) by default; explicitly
   nursery projects -> company 3 (At The Grove Nursery); "Josh - Personal
   Tasks" is EXCLUDED entirely. All open tasks are Josh-or-unassigned, so every
   migrated task lands on Josh with the original assignee + due date noted
   in-body (per-agent Odoo users arrive with WS3c / GOL-2095). Projects whose
   sections are degenerate get default stages Backlog / In Progress / Done.

Mapping: projects -> project.project, sections -> project.task.type (stages),
subtasks -> project.task with parent_id, assignee + due carried, comments/stories
folded into the description with original author + timestamp in-body.

IDEMPOTENCY (re-runnable)
-------------------------
Each migrated task is keyed on its Asana gid. If the Odoo project.task model
carries an `x_asana_gid` char field (see --gid-field), it is used as the match
key; otherwise the gid is embedded as an "Asana-GID: <gid>" marker line at the
foot of the description and matched with a `description like` search. A re-run
updates the existing record in place instead of creating a duplicate.

SAFETY
------
Dry-run is the DEFAULT: without --execute the script only reads the archive and
prints the migration manifest (per-project counts, sample mappings, and every
skip with its reason) — no Odoo connection is opened. --execute performs the
writes and REQUIRES the Odoo env contract below. Execution is additionally
gated at the process level on: the WS3a archive existing (its path is this
script's input) AND CEO/board sign-off for prod writes.

ENV CONTRACT (only needed for --execute; inject via the secrets manager, never
in agent config / an issue thread):
    ODOO_URL        base URL, e.g. https://erp.goldberrygrove.farm
    ODOO_DB         database name (falls back to ODOO_DB_NAME)
    ODOO_LOGIN      the API user's login
    ODOO_API_KEY    that user's Odoo API key (XML-RPC password)

WS3a ARCHIVE INPUT CONTRACT (the shape this script consumes internally)
-----------------------------------------------------------------------
{
  "generated_at": "<ISO8601>",
  "workspace_gid": "1213817682522376",
  "users": [ {"gid","name","email"} ],
  "projects": [
    {
      "gid","name","archived": bool,
      "team": {"name"} | null,
      "sections": [ {"gid","name"} ],          # in board order
      "tasks": [
        {
          "gid","name","notes","completed": bool,
          "assignee": {"gid","name","email"} | null,
          "due_on": "YYYY-MM-DD" | null,
          "due_at": "<ISO8601>" | null,
          "created_at": "<ISO8601>",
          "parent": {"gid"} | null,            # set for subtasks
          "section_gid": "<gid>" | null,        # section within THIS project
          "memberships": [ {"section": {"gid","name"}} ],  # fallback for section
          "custom_fields": [ {"name","display_value"} ],
          "stories": [ {"created_at","created_by":{"name"},"text",
                        "resource_subtype"} ]   # comment_added folded in-body
        }
      ]
    }
  ]
}

ARCHIVE ON DISK (what GOL-2093 actually wrote to Spaces) vs. the shape above:
the WS3a export is a SPLIT directory, not one merged file —
    manifest.json                     # counts + per-project {file: ...} index
    workspace.json                    # {users, teams, projects_index, ...}
    projects/project-<gid>.json       # {project: {...meta}, sections, tasks}
        - project metadata (gid/name/archived/team) is nested under "project"
        - subtasks are nested under each task's "subtasks" array, NOT flattened
`load_archive` normalizes that layout into the single in-memory shape above
(lifting project meta to the top level and depth-first-flattening subtasks into
`tasks` with their `parent.gid` intact) so build_plan/execute_plan never see the
difference. A pre-merged single JSON file in the shape above is also accepted.

USAGE
-----
    # dry-run (default) — prints manifest, no writes, no Odoo needed.
    # --input is EITHER the split WS3a archive dir (or its manifest.json) OR a
    # single merged JSON in the contract shape above:
    asana_to_odoo_migrate.py --input ./ws3a-archive/
    asana_to_odoo_migrate.py --input asana_export.json

    # write to Odoo (gated on archive existing + CEO/board approval):
    ODOO_URL=… ODOO_DB=… ODOO_LOGIN=… ODOO_API_KEY=… \
        asana_to_odoo_migrate.py --input ./ws3a-archive/ --execute --confirm
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

# --------------------------------------------------------------------------- #
# Config — the single source of truth for the ratified mapping rules.
# --------------------------------------------------------------------------- #

# Asana project gid -> migration target.
#   company: 1 (Goldberry Grove Farm) | 3 (At The Grove Nursery) | None (exclude)
#   review:  optional note surfaced in the manifest for a human eyeball.
# Enumerated 2026-09-05 from the live workspace (9 active projects). Any project
# gid not listed here falls through to DEFAULT_COMPANY with a review flag so a
# newly-created Asana project can never be silently mis-scoped.
PROJECT_MAP: dict[str, dict] = {
    "1213824483978728": {"name": "Goldberry Grove | Content calendar", "company": 1},
    "1213867393569940": {"name": "Gather at the Grove | Dev", "company": 1},
    "1213898117964570": {"name": "Goldberry Grove Operations (Nursery & Orchard)", "company": 3},
    "1213898907106904": {"name": "Josh - Personal Tasks", "company": None},  # excluded
    "1213903263906001": {"name": "Gather at the Grove | Odoo ERP Features and Requests",
                          "company": 3,
                          "review": "CEO rule scoped 'ERP nursery features' to co3; "
                                    "confirm at execution — remap co1 if not nursery."},
    "1213912315251614": {"name": "Social Media Content Pipeline", "company": 1},
    "1214811822631642": {"name": "AgriforestryOS | Dev Sprints", "company": 1},
    "1214851151154315": {"name": "AgenticOS", "company": 1},
    "1216085335628887": {"name": "Gather at the Grove | Observability", "company": 1},
}

DEFAULT_COMPANY = 1
DEFAULT_STAGES = ["Backlog", "In Progress", "Done"]

# Rule 1 — dedupe: sections whose tasks are Paperclip twins by construction.
SKIP_SECTIONS = {"QA Round — Tester Feedback", "QA Round - Tester Feedback"}
# A GOL-xxx reference marks a Paperclip twin. GATH-xxx is Asana-native, not a twin.
GOL_REF_RE = re.compile(r"\bGOL-\d+\b")

# Rule 2 — prune: names that are self-referential / migration bookkeeping.
NAME_SKIP_RES = [
    re.compile(r"migrate\s+this\s+asana\s+board", re.I),
    re.compile(r"\basana\s*->\s*odoo\b", re.I),
]

# Idempotency marker embedded in the description when no x_asana_gid field.
GID_MARKER_PREFIX = "Asana-GID:"
FOLD_HEADER = "--- Imported from Asana (WS3b) ---"


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def _log(*args: object) -> None:
    print("[asana->odoo]", *args, file=sys.stderr, flush=True)


def _die(msg: str, code: int = 1) -> "NoReturn":  # type: ignore[name-defined]
    _log("ERROR:", msg)
    sys.exit(code)


def _parse_due(due_on: str | None) -> dt.date | None:
    if not due_on:
        return None
    try:
        return dt.date.fromisoformat(due_on[:10])
    except ValueError:
        return None


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


# --------------------------------------------------------------------------- #
# Pure transform layer (unit-tested; no network)
# --------------------------------------------------------------------------- #

def project_target(project: dict) -> dict:
    """Resolve a project to its migration target per the company-scoping rule.

    Returns {"action": "migrate", "company": int, "odoo_name": str, "review": str|None}
         or {"action": "exclude", "reason": str}.
    """
    gid = str(project.get("gid", ""))
    name = (project.get("name") or "").strip()
    cfg = PROJECT_MAP.get(gid)
    if cfg is None:
        return {
            "action": "migrate",
            "company": DEFAULT_COMPANY,
            "odoo_name": name or f"Asana {gid}",
            "review": "project not in ratified map — defaulted to company 1; confirm scoping.",
        }
    if cfg.get("company") is None:
        return {"action": "exclude", "reason": "excluded by ratified rule (Personal)"}
    return {
        "action": "migrate",
        "company": int(cfg["company"]),
        "odoo_name": (cfg.get("name") or name).strip(),
        "review": cfg.get("review"),
    }


def section_for_task(task: dict, sections_by_gid: dict[str, str]) -> str | None:
    """The section name for a task within its project, or None if degenerate."""
    sgid = task.get("section_gid")
    if not sgid:
        for m in task.get("memberships") or []:
            sec = (m or {}).get("section") or {}
            if sec.get("gid"):
                sgid = sec.get("gid")
                if sec.get("name"):
                    return sec["name"]
                break
    if sgid and sgid in sections_by_gid:
        return sections_by_gid[sgid]
    return None


def is_paperclip_twin(task: dict, section_name: str | None) -> bool:
    """Rule 1 — dedupe: task already exists as a GOL issue / QA-feedback twin."""
    if section_name and section_name in SKIP_SECTIONS:
        return True
    haystack = f"{task.get('name', '')}\n{task.get('notes', '')}"
    return bool(GOL_REF_RE.search(haystack))


def prune_reason(task: dict, today: dt.date) -> str | None:
    """Rule 2 — prune: return a skip reason, or None to keep.

    Keep iff the task is active (not completed) AND (undated OR due today/later).
    """
    if task.get("completed"):
        return "completed"
    name = task.get("name") or ""
    if any(rx.search(name) for rx in NAME_SKIP_RES):
        return "self-referential migration task"
    due = _parse_due(task.get("due_on"))
    if due is not None and due < today:
        return f"past-due ({due.isoformat()})"
    return None


def classify_task(task: dict, sections_by_gid: dict[str, str], today: dt.date) -> dict:
    """Decide a single task's fate. Returns {'action','reason','section'}."""
    section_name = section_for_task(task, sections_by_gid)
    if is_paperclip_twin(task, section_name):
        return {"action": "skip", "reason": "dedupe: Paperclip twin", "section": section_name}
    pr = prune_reason(task, today)
    if pr is not None:
        return {"action": "skip", "reason": f"prune: {pr}", "section": section_name}
    return {"action": "migrate", "reason": None, "section": section_name}


def comment_stories(stories: list[dict] | None) -> list[dict]:
    """Keep only human comments; drop Asana system stories (assigned, etc.)."""
    out = []
    for s in stories or []:
        sub = s.get("resource_subtype") or s.get("type")
        text = (s.get("text") or "").strip()
        if not text:
            continue
        # comment_added is the comment subtype; older exports use type == 'comment'.
        if sub in ("comment_added", "comment"):
            out.append(s)
    return out


def fold_description(task: dict, gid_field_present: bool) -> str:
    """Build the Odoo task description: notes + provenance + folded comments.

    When no x_asana_gid field exists, the gid marker line anchors idempotency.
    """
    parts: list[str] = []
    notes = (task.get("notes") or "").strip()
    if notes:
        parts.append(notes)

    prov: list[str] = [FOLD_HEADER]
    assignee = task.get("assignee") or {}
    if assignee.get("name"):
        prov.append(f"Original Asana assignee: {assignee['name']}"
                    + (f" <{assignee['email']}>" if assignee.get("email") else ""))
    if task.get("due_on"):
        prov.append(f"Original due date: {task['due_on']}")
    for cf in task.get("custom_fields") or []:
        if cf.get("display_value"):
            prov.append(f"{cf.get('name', 'field')}: {cf['display_value']}")

    comments = comment_stories(task.get("stories"))
    if comments:
        prov.append("")
        prov.append("Comments (folded from Asana):")
        for c in comments:
            who = (c.get("created_by") or {}).get("name") or "unknown"
            when = (c.get("created_at") or "")[:16].replace("T", " ")
            prov.append(f"  [{who} · {when}] {c['text'].strip()}")

    if not gid_field_present:
        prov.append("")
        prov.append(f"{GID_MARKER_PREFIX} {task.get('gid')}")

    parts.append("\n".join(prov))
    return "\n\n".join(p for p in parts if p).strip()


def stage_names_for_project(project: dict, kept_tasks: list[dict],
                            sections_by_gid: dict[str, str]) -> list[str]:
    """Ordered stage names for a project: its sections, else the default set."""
    ordered = [s.get("name") for s in (project.get("sections") or []) if s.get("name")]
    ordered = [n for n in ordered if n not in SKIP_SECTIONS]
    if ordered:
        return ordered
    # Degenerate project — but still surface any ad-hoc section a kept task names.
    seen: list[str] = []
    for t in kept_tasks:
        sec = section_for_task(t, sections_by_gid)
        if sec and sec not in seen and sec not in SKIP_SECTIONS:
            seen.append(sec)
    return seen or list(DEFAULT_STAGES)


# --------------------------------------------------------------------------- #
# Planning — turn an archive into a migration manifest (no writes)
# --------------------------------------------------------------------------- #

def build_plan(archive: dict, today: dt.date, gid_field_present: bool) -> dict:
    """Produce the full migration plan/manifest from the archive."""
    projects_out: list[dict] = []
    totals = {"projects_migrate": 0, "projects_excluded": 0,
              "tasks_migrate": 0, "tasks_subtasks": 0, "tasks_skipped": 0,
              "comments_folded": 0}

    for project in archive.get("projects") or []:
        if project.get("archived"):
            # active projects only (scope); note but don't migrate.
            projects_out.append({"asana_name": project.get("name"),
                                 "action": "exclude", "reason": "archived project"})
            totals["projects_excluded"] += 1
            continue

        target = project_target(project)
        if target["action"] == "exclude":
            projects_out.append({"asana_name": project.get("name"),
                                 "action": "exclude", "reason": target["reason"]})
            totals["projects_excluded"] += 1
            continue

        sections_by_gid = {str(s["gid"]): s["name"]
                           for s in (project.get("sections") or []) if s.get("gid")}

        kept: list[dict] = []
        skipped: list[dict] = []
        seen_names: dict[str, str] = {}  # normalized name -> first gid (exact-dup prune)

        for task in project.get("tasks") or []:
            decision = classify_task(task, sections_by_gid, today)
            if decision["action"] == "skip":
                skipped.append({"gid": task.get("gid"), "name": task.get("name"),
                                "reason": decision["reason"]})
                continue
            norm = _norm_name(task.get("name", ""))
            if norm and norm in seen_names:
                skipped.append({"gid": task.get("gid"), "name": task.get("name"),
                                "reason": f"prune: exact-duplicate of {seen_names[norm]}"})
                continue
            seen_names[norm] = task.get("gid")
            decision["task"] = task
            kept.append(decision)

        stages = stage_names_for_project(project, [d["task"] for d in kept], sections_by_gid)

        tasks_manifest = []
        for d in kept:
            task = d["task"]
            is_sub = bool((task.get("parent") or {}).get("gid"))
            folded_comments = len(comment_stories(task.get("stories")))
            totals["comments_folded"] += folded_comments
            if is_sub:
                totals["tasks_subtasks"] += 1
            tasks_manifest.append({
                "gid": task.get("gid"),
                "name": task.get("name"),
                "stage": d["section"] or (stages[0] if stages else "Backlog"),
                "parent_gid": (task.get("parent") or {}).get("gid"),
                "due_on": task.get("due_on"),
                "orig_assignee": (task.get("assignee") or {}).get("name"),
                "comments_folded": folded_comments,
            })

        totals["projects_migrate"] += 1
        totals["tasks_migrate"] += len(kept)
        totals["tasks_skipped"] += len(skipped)

        projects_out.append({
            "asana_gid": project.get("gid"),
            "asana_name": project.get("name"),
            "action": "migrate",
            "odoo_project": target["odoo_name"],
            "company_id": target["company"],
            "review": target.get("review"),
            "stages": stages,
            "tasks_kept": len(kept),
            "tasks_skipped": len(skipped),
            "tasks": tasks_manifest,
            "skipped": skipped,
        })

    return {
        "generated_from": archive.get("generated_at"),
        "today": today.isoformat(),
        "idempotency": "x_asana_gid field" if gid_field_present else f"{GID_MARKER_PREFIX} marker",
        "totals": totals,
        "projects": projects_out,
    }


# --------------------------------------------------------------------------- #
# Odoo write layer (XML-RPC) — only touched under --execute
# --------------------------------------------------------------------------- #

class Odoo:
    def __init__(self) -> None:
        import xmlrpc.client  # local import: dry-run never needs it
        self._xmlrpc = xmlrpc.client
        self.url = (os.environ.get("ODOO_URL") or "").rstrip("/")
        self.db = os.environ.get("ODOO_DB") or os.environ.get("ODOO_DB_NAME") or ""
        self.login = os.environ.get("ODOO_LOGIN") or ""
        self.api_key = os.environ.get("ODOO_API_KEY") or ""
        missing = [n for n, v in (("ODOO_URL", self.url), ("ODOO_DB", self.db),
                                  ("ODOO_LOGIN", self.login), ("ODOO_API_KEY", self.api_key))
                   if not v]
        if missing:
            _die("missing required env for --execute: " + ", ".join(missing)
                 + " — inject via the secrets manager, never in agent config.")
        self._uid: int | None = None
        self._models = None

    @property
    def uid(self) -> int:
        if self._uid is None:
            common = self._xmlrpc.ServerProxy(f"{self.url}/xmlrpc/2/common")
            uid = common.authenticate(self.db, self.login, self.api_key, {})
            if not uid:
                _die("Odoo authentication failed — check ODOO_LOGIN / ODOO_API_KEY / ODOO_DB.")
            self._uid = int(uid)
        return self._uid

    @property
    def models(self):
        if self._models is None:
            self._models = self._xmlrpc.ServerProxy(f"{self.url}/xmlrpc/2/object")
        return self._models

    def execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        return self.models.execute_kw(self.db, self.uid, self.api_key,
                                      model, method, args, kwargs or {})

    def has_field(self, model: str, field: str) -> bool:
        fg = self.execute(model, "fields_get", [], {"attributes": ["type"]})
        return field in fg

    def find_or_create_project(self, name: str, company_id: int) -> int:
        ids = self.execute("project.project", "search",
                           [[["name", "=", name], ["company_id", "=", company_id]]],
                           {"limit": 1})
        if ids:
            return ids[0]
        _log(f"CREATE project.project name={name!r} company_id={company_id}")
        return self.execute("project.project", "create",
                            [{"name": name, "company_id": company_id}])

    def find_or_create_stage(self, name: str, project_id: int,
                             cache: dict[tuple[int, str], int]) -> int:
        key = (project_id, name)
        if key in cache:
            return cache[key]
        ids = self.execute("project.task.type", "search",
                           [[["name", "=", name], ["project_ids", "in", [project_id]]]],
                           {"limit": 1})
        if ids:
            cache[key] = ids[0]
            return ids[0]
        _log(f"CREATE project.task.type name={name!r} project_id={project_id}")
        sid = self.execute("project.task.type", "create",
                           [{"name": name, "project_ids": [(4, project_id)]}])
        cache[key] = sid
        return sid

    def resolve_user(self, login_or_email: str) -> int | None:
        ids = self.execute("res.users", "search",
                           [["|", ["login", "=", login_or_email],
                             ["email", "=", login_or_email]]], {"limit": 1})
        return ids[0] if ids else None

    def find_task_by_gid(self, gid: str, gid_field: str | None) -> int | None:
        if gid_field:
            ids = self.execute("project.task", "search",
                               [[[gid_field, "=", gid]]], {"limit": 1})
        else:
            ids = self.execute("project.task", "search",
                               [[["description", "like", f"{GID_MARKER_PREFIX} {gid}"]]],
                               {"limit": 1})
        return ids[0] if ids else None


# --------------------------------------------------------------------------- #
# Execution — write the plan into Odoo idempotently (two passes for parents)
# --------------------------------------------------------------------------- #

def execute_plan(archive: dict, plan: dict, odoo: Odoo, gid_field: str | None,
                 assignee_login: str) -> dict:
    tasks_by_gid = {}
    for project in archive.get("projects") or []:
        for t in project.get("tasks") or []:
            tasks_by_gid[str(t.get("gid"))] = t

    assignee_id = odoo.resolve_user(assignee_login)
    if not assignee_id:
        _die(f"could not resolve assignee user {assignee_login!r} in Odoo.")

    gid_field_present = gid_field is not None
    stage_cache: dict[tuple[int, str], int] = {}
    odoo_id_by_gid: dict[str, int] = {}
    created = updated = 0

    # Pass 1: upsert every kept task (parent_id resolved in pass 2).
    for pm in plan["projects"]:
        if pm.get("action") != "migrate":
            continue
        project_id = odoo.find_or_create_project(pm["odoo_project"], pm["company_id"])
        for name in pm["stages"]:
            odoo.find_or_create_stage(name, project_id, stage_cache)

        for tm in pm["tasks"]:
            task = tasks_by_gid.get(str(tm["gid"]))
            if task is None:
                continue
            stage_id = odoo.find_or_create_stage(
                tm["stage"] or pm["stages"][0], project_id, stage_cache)
            values = {
                "name": task.get("name") or "(untitled)",
                "project_id": project_id,
                "stage_id": stage_id,
                "user_ids": [(6, 0, [assignee_id])],
                "description": fold_description(task, gid_field_present),
            }
            if task.get("due_on"):
                values["date_deadline"] = task["due_on"]
            if gid_field_present:
                values[gid_field] = str(task.get("gid"))

            existing = odoo.find_task_by_gid(str(task.get("gid")), gid_field)
            if existing:
                odoo.execute("project.task", "write", [[existing], values])
                odoo_id_by_gid[str(task.get("gid"))] = existing
                updated += 1
            else:
                new_id = odoo.execute("project.task", "create", [values])
                odoo_id_by_gid[str(task.get("gid"))] = new_id
                created += 1

    # Pass 2: link subtasks to their parents (only when the parent migrated too).
    linked = 0
    for gid, oid in odoo_id_by_gid.items():
        parent_gid = (tasks_by_gid.get(gid, {}).get("parent") or {}).get("gid")
        if parent_gid and str(parent_gid) in odoo_id_by_gid:
            odoo.execute("project.task", "write",
                         [[oid], {"parent_id": odoo_id_by_gid[str(parent_gid)]}])
            linked += 1

    return {"created": created, "updated": updated, "subtasks_linked": linked,
            "assignee_id": assignee_id}


# --------------------------------------------------------------------------- #
# Archive loading — normalize the split WS3a export into the input-contract shape
# --------------------------------------------------------------------------- #

def _flatten_tasks(tasks: list) -> list:
    """Depth-first flatten of nested `subtasks` into one flat task list.

    The WS3a export nests subtasks under each task's `subtasks` array; build_plan
    expects every task (parents and subtasks alike) as a flat entry in `tasks`,
    detecting subtasks by their `parent.gid`. Each exported subtask already
    carries its `parent`, so the flat list preserves parent linkage. The nested
    `subtasks` payload is dropped from the emitted parent so the flat list is the
    single source of truth (no double-counting downstream).
    """
    out: list[dict] = []
    for task in tasks or []:
        subs = task.get("subtasks") or []
        out.append({k: v for k, v in task.items() if k != "subtasks"})
        if subs:
            out.extend(_flatten_tasks(subs))
    return out


def _normalize_project_doc(doc: dict) -> dict:
    """One split `projects/project-<gid>.json` -> the merged project shape.

    The export nests project metadata under a `project` sub-object and keeps
    subtasks nested; lift the metadata to the top level and flatten the tasks.
    """
    meta = doc.get("project") or {}
    return {
        "gid": meta.get("gid"),
        "name": meta.get("name"),
        "archived": bool(meta.get("archived")),
        "team": meta.get("team"),
        "sections": doc.get("sections") or [],
        "tasks": _flatten_tasks(doc.get("tasks") or []),
    }


def _assemble_split_archive(root: str, manifest: dict | None = None) -> dict:
    """Assemble the split WS3a archive under `root` into the merged input shape.

    Layout: manifest.json (per-project {file} index) + workspace.json (users) +
    projects/project-<gid>.json. Missing manifest -> not a WS3a archive dir.
    """
    if manifest is None:
        mpath = os.path.join(root, "manifest.json")
        if not os.path.exists(mpath):
            _die(f"{root!r} is not a WS3a archive dir (no manifest.json)")
        with open(mpath, encoding="utf-8") as fh:
            manifest = json.load(fh)

    users: list = []
    wpath = os.path.join(root, "workspace.json")
    if os.path.exists(wpath):
        with open(wpath, encoding="utf-8") as fh:
            users = json.load(fh).get("users") or []

    projects: list = []
    for entry in manifest.get("projects") or []:
        rel = entry.get("file")
        if not rel:
            continue
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            projects.append(_normalize_project_doc(json.load(fh)))

    return {
        "generated_at": manifest.get("generated_at"),
        "workspace_gid": (manifest.get("workspace") or {}).get("gid"),
        "users": users,
        "projects": projects,
    }


def load_archive(path: str) -> dict:
    """Read the WS3a archive from either the split directory the GOL-2093 export
    produced (a dir, or its manifest.json) or a single pre-merged JSON file in
    the input-contract shape. Always returns the merged in-memory shape.
    """
    if os.path.isdir(path):
        return _assemble_split_archive(path)
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    # Pointed straight at a split-archive manifest.json? Assemble from its dir.
    proj = doc.get("projects")
    if isinstance(proj, list) and proj and all(
            isinstance(p, dict) and "file" in p for p in proj):
        return _assemble_split_archive(os.path.dirname(os.path.abspath(path)),
                                       manifest=doc)
    return doc


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _today(args) -> dt.date:
    if args.today:
        return dt.date.fromisoformat(args.today)
    # Injected for determinism/testing; falls back to the system date otherwise.
    return dt.date.today()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="asana_to_odoo_migrate.py",
        description="WS3b Asana -> Odoo project.task migration (GOL-2094).")
    p.add_argument("--input", required=True,
                   help="WS3a archive: the split export dir (or its manifest.json), "
                        "or a single merged JSON in the input-contract shape")
    p.add_argument("--execute", action="store_true",
                   help="perform Odoo writes (default: dry-run manifest only)")
    p.add_argument("--confirm", action="store_true",
                   help="required alongside --execute to actually mutate Odoo")
    p.add_argument("--assignee-login", default="josh@goldberrygrove.farm",
                   help="Odoo login/email every migrated task lands on")
    p.add_argument("--gid-field", default="x_asana_gid",
                   help="project.task field used as the idempotency key if present")
    p.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD)")
    p.add_argument("--output", default=None, help="write manifest JSON to this path")
    args = p.parse_args(argv)

    try:
        archive = load_archive(args.input)
    except (OSError, json.JSONDecodeError) as exc:
        _die(f"could not read archive {args.input!r}: {exc}")

    today = _today(args)

    if not args.execute:
        # Dry-run: no Odoo connection. Assume the marker-based idempotency so the
        # manifest is producible offline (the field, if present, only changes the
        # key mechanism, not the plan).
        plan = build_plan(archive, today, gid_field_present=False)
        out = json.dumps(plan, indent=2, default=str)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(out + "\n")
        print(out)
        _log("DRY-RUN — no Odoo writes. Totals:", json.dumps(plan["totals"]))
        return 0

    if not args.confirm:
        _die("--execute requires --confirm (guards against accidental prod writes).")

    odoo = Odoo()
    gid_field = args.gid_field if odoo.has_field("project.task", args.gid_field) else None
    if gid_field is None:
        _log(f"note: project.task has no {args.gid_field!r} field — "
             f"using '{GID_MARKER_PREFIX} <gid>' description marker for idempotency.")

    plan = build_plan(archive, today, gid_field_present=gid_field is not None)
    result = execute_plan(archive, plan, odoo, gid_field, args.assignee_login)
    plan["execution"] = result
    out = json.dumps(plan, indent=2, default=str)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out + "\n")
    print(out)
    _log("EXECUTED. Result:", json.dumps(result), "Totals:", json.dumps(plan["totals"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
