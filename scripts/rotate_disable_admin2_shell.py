# -*- coding: utf-8 -*-
"""
Rotate or disable the generic built-in Odoo admin (res.users uid=2) fallback
password on prod. GOL-2080 (hardening spun out of GOL-1780).

BACKGROUND
    During the 2026-08-31 CEO prod-Odoo login recovery (GOL-1780) a fallback
    password was set on the generic, built-in administrator account uid=2 to
    regain access. That is a SHARED, NON-ATTRIBUTABLE superuser credential and
    must not persist now that Josh has his own attributable CEO login (a
    separate uid with base.group_system on all companies). This script kills the
    shared fallback credential -- either by DISABLING the account (archive +
    password rotate) when nothing depends on uid=2 for automation, or by
    ROTATING the password to a random value stored nowhere when something does.

MECHANISM (same superuser odoo-shell path as GOL-1780 / GOL-89, CEO-ratified)
    set -a; . /etc/grove/.env; set +a
    cd /etc/grove && docker compose exec -T \
        -e ADMIN2_MODE=report \
        odoo odoo shell -d "$DB_NAME" --no-http --logfile=/dev/null \
        < scripts/rotate_disable_admin2_shell.py

    Inside `odoo shell`, `env` runs as SUPERUSER, so no admin login/API key is
    needed. The single prod droplet runs one docker-compose.yml under /etc/grove
    (no override files, no /opt/grove); DB_NAME comes from /etc/grove/.env.
    (Same layout the CEO-login provisioner confirmed live 2026-08-31.)

MODES (ADMIN2_MODE)
    report   (default) -- READ-ONLY. Print uid=2's posture (login, active, is a
             password live, is it still an admin) and enumerate every automation
             binding on uid=2 (API keys, scheduled actions). Writes nothing, no
             commit. This answers GOL-2080 questions #1 (is the password live)
             and #3 (does any automation authenticate as uid=2). ALWAYS run this
             first; the output drives which write mode to run next.
    disable  -- Archive the account (active=False) AND rotate its password to a
             random value that is discarded (never printed/stored), so even a
             later reactivation cannot reuse the old fallback. REFUSES if any
             automation binding is found on uid=2 (would break it) -- use rotate
             instead in that case. Preferred per GOL-2080 when deps == 0.
    rotate   -- Rotate the password to a random discarded value only; leave
             active=True so any automation running AS uid=2 keeps working. Kills
             the known shared fallback secret without changing account state.

CREDENTIAL SAFETY -- nothing secret is ever emitted. Both write modes set a
    fresh random password and DISCARD it (it is never printed, logged, or
    returned). The point is only to invalidate the known shared value; no human
    needs the new one because uid=2 is a service/built-in account, not a login
    anyone should use. Josh keeps his own attributable CEO login.

GUARDS (fail loud, never touch the wrong account)
    * uid=1 (OdooBot / base.user_root) is NEVER touched.
    * The target is refused if its login matches the CEO login env CEO_LOGIN
      (default josh@goldberrygrove.farm) -- we must not disable Josh.
    * The target is refused if uid=2 does not resolve to a real res.users row.

IDEMPOTENT -- re-running report is read-only; re-running disable/rotate just
    re-ensures the end state (archived and/or a fresh random password). Safe to
    re-run.
"""

import os
import secrets
import string
import sys

ADMIN2_UID = 2
CEO_LOGIN = os.environ.get("CEO_LOGIN", "josh@goldberrygrove.farm").strip()
# report (read-only, default) | disable (archive + rotate) | rotate (rotate only)
MODE = (os.environ.get("ADMIN2_MODE", "report").strip().lower() or "report")


def _err(*a):
    print("[rotate-admin2]", *a, file=sys.stderr, flush=True)


# `env` is injected by `odoo shell`. Fail loudly if run the wrong way.
try:
    env  # noqa: F821  (provided by the shell namespace)
except NameError:
    _err(
        "ERROR: `env` is not defined. Run this INSIDE an Odoo shell, e.g.\n"
        "  docker compose ... exec -T -e ADMIN2_MODE=report odoo odoo shell "
        "-d \"$DB_NAME\" --no-http < scripts/rotate_disable_admin2_shell.py"
    )
    raise SystemExit(2)


def _password_live(uid):
    """Authoritatively report whether uid has a login password set.

    res.users.password is write-only in the ORM (reading it returns ''), so we
    read the stored hash column directly. A non-empty value means a password is
    live and can be used to log in interactively."""
    env.cr.execute("SELECT password FROM res_users WHERE id = %s", (uid,))
    row = env.cr.fetchone()
    if not row:
        return None  # no such row
    return bool((row[0] or "").strip())


def _automation_bindings(uid):
    """Enumerate every place automation authenticates/runs AS uid.

    Returns a dict of {label: count/list}. A non-empty result means DISABLE
    (active=False) would break something, so the caller must ROTATE instead.

      api_keys   res.users.apikeys rows owned by uid -- these are how an
                 integration authenticates over XML-RPC/JSON-RPC as this user.
                 (GOL-2080 note: the Grove API service keys live under a
                 SEPARATE service user via odoo_api_keys_tf_json, not uid=2 --
                 this check verifies that assumption on the live DB.)
      crons      ir.cron scheduled actions whose user_id is uid -- they execute
                 in that user's context; an archived user's crons are skipped.
    """
    bindings = {}

    ApiKeys = env["res.users.apikeys"].sudo() if "res.users.apikeys" in _model_names() else None
    if ApiKeys is not None:
        bindings["api_keys"] = ApiKeys.search_count([("user_id", "=", uid)])
    else:
        bindings["api_keys"] = 0
        _err("WARN: res.users.apikeys model not present; api-key check skipped")

    crons = env["ir.cron"].sudo().search([("user_id", "=", uid)])
    bindings["crons"] = crons  # recordset; len() + names printed by caller

    return bindings


def _model_names():
    # env has no public "does model exist"; registry keys are the model names.
    try:
        return set(env.registry.models.keys())
    except Exception:  # noqa: BLE001 - be permissive across versions
        return set(env.keys()) if hasattr(env, "keys") else set()


def _random_password():
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(32))


def _report(user, pw_live, bindings):
    api_keys = bindings["api_keys"]
    crons = bindings["crons"]
    n_crons = len(crons)
    is_admin = user.has_group("base.group_system") if user else False
    print("----BEGIN ADMIN2_REPORT----")
    print(f"uid={ADMIN2_UID}")
    print(f"login={user.login!r}")
    print(f"name={user.name!r}")
    print(f"active={user.active}")
    print(f"share={user.share}")
    print(f"is_system_admin={is_admin}")
    print(f"password_live={pw_live}")
    print(f"api_keys_owned={api_keys}")
    print(f"crons_running_as_uid2={n_crons}")
    for c in crons:
        print(f"  cron[{c.id}]={c.name!r} active={c.active}")
    deps = (api_keys or 0) + n_crons
    print(f"automation_dependencies_total={deps}")
    print(
        "recommended_action="
        + ("disable (no automation depends on uid=2)" if deps == 0
           else "rotate (automation depends on uid=2; do NOT archive)")
    )
    print("----END ADMIN2_REPORT----")
    return deps


def main():
    if ADMIN2_UID == 1:
        _err("ERROR: refusing to touch uid=1 (OdooBot / base.user_root).")
        return 3

    user = env["res.users"].sudo().browse(ADMIN2_UID).exists()
    if not user:
        _err(f"ERROR: uid={ADMIN2_UID} does not resolve to a res.users row -- wrong DB?")
        return 3
    if user.login and user.login.strip().lower() == CEO_LOGIN.lower():
        _err(
            f"ERROR: uid={ADMIN2_UID} login is the CEO login ({CEO_LOGIN}); "
            "refusing to disable/rotate the CEO account."
        )
        return 3

    pw_live = _password_live(ADMIN2_UID)
    bindings = _automation_bindings(ADMIN2_UID)
    deps = _report(user, pw_live, bindings)

    if MODE == "report":
        _err("MODE=report: read-only, no write, no commit.")
        return 0

    if MODE not in ("disable", "rotate"):
        _err(f"ERROR: unknown ADMIN2_MODE={MODE!r} (want report|disable|rotate).")
        return 2

    if MODE == "disable" and deps > 0:
        _err(
            f"ERROR: MODE=disable but {deps} automation binding(s) depend on "
            f"uid={ADMIN2_UID} (api_keys={bindings['api_keys']}, "
            f"crons={len(bindings['crons'])}). Archiving would break them. "
            "Re-run with ADMIN2_MODE=rotate to rotate the password only."
        )
        return 4

    # Both write modes rotate the password to a fresh random value that is
    # DISCARDED -- it invalidates the known shared fallback and is never emitted.
    new_pw = _random_password()
    write_vals = {"password": new_pw}
    if MODE == "disable":
        write_vals["active"] = False

    try:
        user.write(write_vals)
        env.cr.commit()
    except Exception as exc:  # noqa: BLE001 - surface the reason, don't crash silently
        env.cr.rollback()
        _err(f"ERROR: write failed ({exc!r}); no change committed.")
        return 5
    finally:
        new_pw = None  # drop the plaintext reference promptly

    # Re-read post-state for a truthful confirmation (no secret).
    user = env["res.users"].sudo().browse(ADMIN2_UID)
    pw_live_after = _password_live(ADMIN2_UID)
    print("----BEGIN ADMIN2_RESULT----")
    print(f"mode={MODE}")
    print(f"uid={ADMIN2_UID}")
    print(f"active_after={user.active}")
    print(f"password_rotated=True")
    print(f"password_live_after={pw_live_after}")
    print(
        "note="
        + ("account archived + password rotated to a discarded random value"
           if MODE == "disable"
           else "password rotated to a discarded random value; account left active")
    )
    print("----END ADMIN2_RESULT----")
    _err(
        "Done. The previous shared fallback password on uid=2 is now invalid. "
        "No new password was printed or stored -- uid=2 is not an interactive "
        "login anyone should use. Josh keeps his own attributable CEO login."
    )
    return 0


raise SystemExit(main())
