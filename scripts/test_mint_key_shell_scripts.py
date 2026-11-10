#!/usr/bin/env python3
"""Regression tests for the three `odoo shell` API-key mint scripts.

    scripts/mint_agent_key_shell.py
    scripts/mint_storefront_keys_shell.py
    skills/odoo-logistics/scripts/mint_logistics_key.py

No network, no real Odoo. Each script's source is exec'd with a fake `env` that
models the one Odoo 19 rule that broke prod on 2026-11-10:

  res.users.apikeys._generate() runs _check_expiration_date(), which allows a
  NON-expiring key only when env.is_system() -- i.e. the env is in sudo, or the
  acting user is an administrator. with_user(user) DROPS the shell's superuser
  flag, so `with_user(user)._generate(..., expiration_date=False)` raises
  "The API key must have an expiration date" for any non-admin owner (it did
  for svc-discord-bridge, uid 14). with_user(user).sudo() keeps uid = user and
  restores su, so the key is minted AND still owned by that user.

Properties pinned:

  fake-models-the-rule    the fake itself refuses a non-sudo, non-admin,
                          non-expiring mint (otherwise every other check is
                          vacuous).
  agent-non-admin         mint_agent_key_shell.py mints for a non-admin user,
                          and the key is owned by that user, not the shell's
                          superuser.
  logistics-non-admin     same for mint_logistics_key.py (logistics-otto).
  storefront-non-admin    mint_storefront_keys_shell.py with a non-admin
                          OWNER_LOGIN mints every tenant key, owned by it.
  storefront-admin        the default admin owner still works (unchanged path).

    python3 scripts/test_mint_key_shell_scripts.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
AGENT = os.path.join(_ROOT, "scripts", "mint_agent_key_shell.py")
STOREFRONT = os.path.join(_ROOT, "scripts", "mint_storefront_keys_shell.py")
LOGISTICS = os.path.join(_ROOT, "skills", "odoo-logistics", "scripts", "mint_logistics_key.py")

SUPERUSER_ID = 1


class ValidationError(Exception):
    pass


class _Empty:
    """An empty recordset: falsy, no ids."""

    ids: list = []

    def __bool__(self):
        return False

    def __len__(self):
        return 0


class _User:
    def __init__(self, uid: int, login: str, admin: bool = False, active: bool = True):
        self.id = uid
        self.login = login
        self.admin = admin
        self.active = active


class _UsersModel:
    def __init__(self, env):
        self._env = env

    def sudo(self):
        return self

    def search(self, domain, limit=None):
        want = {field: value for field, _op, value in domain}
        return self._env.users.get(want.get("login")) or _Empty()


class _ApikeysModel:
    """res.users.apikeys with Odoo 19's env semantics: with_user() sets the uid
    and clears su; sudo() sets su and keeps the uid."""

    def __init__(self, env, uid: int, su: bool):
        self._env, self.uid, self.su = env, uid, su

    def sudo(self):
        return _ApikeysModel(self._env, self.uid, True)

    def with_user(self, user):
        return _ApikeysModel(self._env, user.id, False)

    def search(self, domain):
        return _Empty()

    def invalidate_model(self):
        pass

    def _is_system(self) -> bool:
        if self.su or self.uid == SUPERUSER_ID:
            return True
        user = next((u for u in self._env.users.values() if u.id == self.uid), None)
        return bool(user and user.admin)

    def _generate(self, scope, name, expiration_date):
        if not self._is_system() and not expiration_date:
            raise ValidationError("The API key must have an expiration date")
        key = f"k{len(self._env.minted):039d}"
        self._env.minted.append({"owner": self.uid, "scope": scope, "name": name})
        return key


class _Cr:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True

    def execute(self, *_a, **_k):
        pass


class _Env:
    def __init__(self, users):
        self.users = {u.login: u for u in users}
        self.minted: list[dict] = []
        self.cr = _Cr()

    def __getitem__(self, model):
        if model == "res.users":
            return _UsersModel(self)
        if model == "res.users.apikeys":
            # `odoo shell` binds env to the superuser, in sudo.
            return _ApikeysModel(self, SUPERUSER_ID, True)
        raise KeyError(model)


ADMIN = _User(2, "admin", admin=True)
SVC_STOREFRONT = _User(11, "svc-storefront")
OTTO = _User(13, "logistics-otto")
BRIDGE = _User(14, "svc-discord-bridge")


def _run(path: str, env_vars: dict[str, str]):
    """Exec a mint script as `odoo shell` would. Returns (exit, stdout, stderr, env)."""
    env = _Env([ADMIN, SVC_STOREFRONT, OTTO, BRIDGE])
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    saved = {k: os.environ.get(k) for k in env_vars}
    os.environ.update(env_vars)
    out, err = io.StringIO(), io.StringIO()
    code = None
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                exec(compile(src, path, "exec"), {"env": env, "__name__": "__main__"})
            except SystemExit as exc:
                code = exc.code
            except ValidationError as exc:
                code = f"ValidationError: {exc}"
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return code, out.getvalue(), err.getvalue(), env


def test_fake_models_the_rule():
    env = _Env([BRIDGE])
    plain = env["res.users.apikeys"].with_user(BRIDGE)
    try:
        plain._generate("rpc", "x", expiration_date=False)
    except ValidationError:
        pass
    else:
        raise AssertionError("fake must refuse a non-sudo, non-admin, non-expiring mint")
    env["res.users.apikeys"].with_user(BRIDGE).sudo()._generate("rpc", "x", expiration_date=False)
    assert env.minted == [{"owner": 14, "scope": "rpc", "name": "x"}], env.minted
    print("ok  fake-models-the-rule")


def test_agent_non_admin():
    code, out, err, env = _run(AGENT, {"AGENT_LOGIN": "svc-discord-bridge",
                                       "AGENT_KEY_NAME": "grove-discord-bridge"})
    assert code == 0, f"exit {code!r}\nstderr:\n{err}"
    assert env.minted == [{"owner": 14, "scope": "rpc", "name": "grove-discord-bridge"}], env.minted
    assert env.cr.committed
    assert "----BEGIN AGENT_API_KEY----" in out
    print("ok  agent-non-admin")


def test_logistics_non_admin():
    code, out, err, env = _run(LOGISTICS, {})
    assert code == 0, f"exit {code!r}\nstderr:\n{err}"
    assert env.minted == [{"owner": 13, "scope": "rpc", "name": "otto-logistics-runtime"}], env.minted
    assert "----BEGIN LOGISTICS_OTTO_API_KEY----" in out
    print("ok  logistics-non-admin")


def _storefront_json(out: str) -> dict:
    body = out.split("----BEGIN ODOO_API_KEYS_TF_JSON----", 1)[1]
    return json.loads(body.split("----END ODOO_API_KEYS_TF_JSON----", 1)[0])


def test_storefront_non_admin():
    code, out, err, env = _run(STOREFRONT, {"OWNER_LOGIN": "svc-storefront", "TENANTS": ""})
    assert code == 0, f"exit {code!r}\nstderr:\n{err}"
    assert {m["owner"] for m in env.minted} == {11}, env.minted
    assert sorted(_storefront_json(out)) == ["ggg", "goldberry", "nursery"]
    print("ok  storefront-non-admin")


def test_storefront_admin():
    code, out, err, env = _run(STOREFRONT, {"OWNER_LOGIN": "", "TENANTS": "hub"})
    assert code == 0, f"exit {code!r}\nstderr:\n{err}"
    assert env.minted == [{"owner": 2, "scope": None, "name": "grove-storefront-hub"}], env.minted
    assert sorted(_storefront_json(out)) == ["hub"]
    print("ok  storefront-admin")


def main() -> int:
    test_fake_models_the_rule()
    test_agent_non_admin()
    test_logistics_non_admin()
    test_storefront_non_admin()
    test_storefront_admin()
    print("\nall mint key shell script tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
