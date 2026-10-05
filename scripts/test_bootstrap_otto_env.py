#!/usr/bin/env python3
"""Regression tests for skills/odoo-logistics/scripts/bootstrap_otto_env.py (GOL-2963).

No network, no real Odoo, no real Paperclip API. `call()` is monkeypatched with
an in-memory fake agent record, which is enough to pin the four properties that
would silently break Otto's prod Odoo access again:

  sidecar-contract      read_sidecar() returns exactly the four ODOO_* vars and
                        exits non-zero when any one of them is missing or blank,
                        rather than PATCHing a half-populated env that fails at
                        authenticate() time with a confusing error.
  host-header-trap      api_base() targets the in-cluster service and carries
                        the PUBLIC hostname as a Host header. The public URL is
                        Cloudflare-Access-gated and 302s for agent runs, so
                        hitting it directly silently redirects instead of
                        PATCHing anything.
  sibling-env-preserved the PATCH body nests under adapterConfig.env and merges
                        the pre-existing env keys. adapterConfig merges SHALLOWLY
                        at the top level, so sending a bare {"env": {...4 keys}}
                        would drop every other secret the agent already had.
  secret-not-logged     the progress line redacts ODOO_API_KEY. The whole point
                        of the sidecar hand-off is that the credential never
                        reaches a log, a comment, or an issue thread.
  sidecar-write-0600    provision_logistics_user._write_sidecar() creates the
                        file 0600 from the open() flags (never briefly
                        world-readable), round-trips cleanly into
                        read_sidecar(), and does NOT print the password. An
                        earlier version echoed it to stdout between BEGIN/END
                        markers, which in an agent run puts the credential in
                        the run transcript — CodeQL
                        py/clear-text-logging-sensitive-data, alert 576.

    python3 scripts/test_bootstrap_otto_env.py
"""

from __future__ import annotations

import importlib.util
import io
import os
import stat
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SKILL_SCRIPTS = os.path.join(os.path.dirname(_HERE), "skills", "odoo-logistics", "scripts")
_MODULE_PATH = os.path.join(_SKILL_SCRIPTS, "bootstrap_otto_env.py")
_PROVISION_PATH = os.path.join(_SKILL_SCRIPTS, "provision_logistics_user.py")

FULL_SIDECAR = (
    "ODOO_URL=https://odoo.gatheringatthegrove.com\n"
    "ODOO_DB=odoo\n"
    "ODOO_LOGIN=logistics-otto\n"
    "ODOO_API_KEY=s3cr3t-value-not-a-real-credential\n"
)


def _load(path: str = _MODULE_PATH, name: str = "bootstrap_otto_env"):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_sidecar(body: str) -> str:
    fd, path = tempfile.mkstemp(prefix="sidecar-", suffix=".env")
    with os.fdopen(fd, "w") as f:
        f.write(body)
    return path


def test_sidecar_contract(mod) -> None:
    path = _write_sidecar(FULL_SIDECAR + "IGNORED=yes\n# a comment\n\n")
    try:
        got = mod.read_sidecar(path)
    finally:
        os.remove(path)
    assert set(got) == set(mod.WANTED), f"expected exactly {mod.WANTED}, got {sorted(got)}"
    assert got["ODOO_LOGIN"] == "logistics-otto", got["ODOO_LOGIN"]
    assert got["ODOO_DB"] == "odoo", got["ODOO_DB"]

    # A sidecar that is missing the secret must abort, not PATCH a partial env.
    partial = _write_sidecar(FULL_SIDECAR.replace("ODOO_API_KEY=s3cr3t-value-not-a-real-credential\n", ""))
    try:
        try:
            mod.read_sidecar(partial)
        except SystemExit as e:
            assert e.code != 0, "a sidecar missing ODOO_API_KEY must exit non-zero"
        else:
            raise AssertionError("read_sidecar accepted a sidecar with no ODOO_API_KEY")
    finally:
        os.remove(partial)

    # An absent sidecar is a clean, actionable exit — not a traceback.
    try:
        mod.read_sidecar(os.path.join(tempfile.gettempdir(), "definitely-not-here-gol2963.env"))
    except SystemExit as e:
        assert e.code != 0, "a missing sidecar must exit non-zero"
    else:
        raise AssertionError("read_sidecar accepted a nonexistent path")
    print("ok  sidecar-contract")


def test_host_header_trap(mod) -> None:
    os.environ["PAPERCLIP_API_URL"] = "https://paperclip.gatheringatthegrove.com"
    base, host = mod.api_base()
    assert base.startswith("http://"), f"must not go out over the gated public edge: {base}"
    assert "paperclip-server" in base, f"must target the in-cluster service: {base}"
    assert host == "paperclip.gatheringatthegrove.com", host
    assert host not in base, "the public host belongs in the Host header, not the URL"
    print("ok  host-header-trap")


def test_sibling_env_preserved_and_secret_not_logged(mod) -> None:
    path = _write_sidecar(FULL_SIDECAR)
    calls: list[tuple[str, str, dict | None]] = []
    agent = {
        "id": "agent-under-test",
        "adapterConfig": {
            "env": {"OP_SERVICE_ACCOUNT_TOKEN": {"type": "plain", "value": "pre-existing"}},
            "timeoutSec": 1800,
        },
    }

    def fake_call(method, p, body=None):
        calls.append((method, p, body))
        if method == "GET":
            return 200, agent
        if method == "PATCH":
            # Mimic the server's SHALLOW top-level merge of adapterConfig.
            agent["adapterConfig"] = {**agent["adapterConfig"], **(body or {}).get("adapterConfig", {})}
            return 200, agent
        raise AssertionError(f"unexpected {method} {p}")

    mod.call = fake_call
    mod.inline_check = lambda values: 0  # no network, no odoo_client subprocess

    os.environ["PAPERCLIP_AGENT_ID"] = "agent-under-test"
    os.environ["PAPERCLIP_API_KEY"] = "fake"
    err = io.StringIO()
    real_stderr, sys.stderr = sys.stderr, err
    real_argv, sys.argv = sys.argv, ["bootstrap_otto_env.py", "--sidecar", path]
    try:
        rc = mod.main()
    finally:
        sys.stderr = real_stderr
        sys.argv = real_argv
        if os.path.exists(path):
            os.remove(path)

    assert rc == 0, f"main() returned {rc}: {err.getvalue()}"

    patches = [c for c in calls if c[0] == "PATCH"]
    assert len(patches) == 1, f"expected exactly one PATCH, got {len(patches)}"
    body = patches[0][2] or {}
    assert set(body) == {"adapterConfig"}, f"PATCH body must nest under adapterConfig only: {sorted(body)}"
    env_block = body["adapterConfig"]["env"]
    for k in mod.WANTED:
        assert env_block[k] == {"type": "plain", "value": _expected(k)}, (k, env_block[k])
    assert "OP_SERVICE_ACCOUNT_TOKEN" in env_block, (
        "the pre-existing env key was dropped — adapterConfig merges shallowly, "
        "so the whole env sub-object must be sent merged"
    )
    assert agent["adapterConfig"].get("timeoutSec") == 1800, (
        "a sibling adapterConfig key was dropped by the PATCH"
    )

    log = err.getvalue()
    assert "s3cr3t-value-not-a-real-credential" not in log, "the credential leaked into the log"
    assert "<redacted>" in log, "ODOO_API_KEY must be redacted in the progress line"
    assert not os.path.exists(path), "the sidecar must be shredded after a successful inject"
    print("ok  sibling-env-preserved")
    print("ok  secret-not-logged")


def test_sidecar_write_0600(mod) -> None:
    prov = _load(_PROVISION_PATH, "provision_logistics_user")
    secret = "P" * 40
    directory = tempfile.mkdtemp(prefix="gol2963-")
    path = os.path.join(directory, "nested", "otto-odoo.env")

    out, err = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        prov._write_sidecar(path, "https://odoo.example", "odoo", "logistics-otto", secret)
    finally:
        sys.stdout, sys.stderr = real_out, real_err

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"sidecar must be 0600, got {oct(mode)}"
    assert secret not in out.getvalue(), "the password was printed to stdout"
    assert secret not in err.getvalue(), "the password was printed to stderr"
    assert path in err.getvalue(), "the sidecar path should be logged so the operator can find it"

    # Round-trips into the consumer: the two scripts agree on the file format.
    got = mod.read_sidecar(path)
    assert got == {
        "ODOO_URL": "https://odoo.example",
        "ODOO_DB": "odoo",
        "ODOO_LOGIN": "logistics-otto",
        "ODOO_API_KEY": secret,
    }, got

    os.remove(path)
    os.rmdir(os.path.dirname(path))
    os.rmdir(directory)
    print("ok  sidecar-write-0600")


def _expected(key: str) -> str:
    return {
        "ODOO_URL": "https://odoo.gatheringatthegrove.com",
        "ODOO_DB": "odoo",
        "ODOO_LOGIN": "logistics-otto",
        "ODOO_API_KEY": "s3cr3t-value-not-a-real-credential",
    }[key]


def main() -> int:
    mod = _load()
    test_sidecar_contract(mod)
    test_host_header_trap(mod)
    test_sidecar_write_0600(mod)
    test_sibling_env_preserved_and_secret_not_logged(_load())
    print("\nall bootstrap_otto_env tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
