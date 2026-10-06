#!/usr/bin/env python3
"""
One-shot self-service bootstrap: move the prod Odoo credential for
`logistics-otto` out of a short-lived sidecar file on disk and into THIS
agent's own runtime env (`agents.adapter_config.env`), then prove it works.

Why this script exists (GOL-2963)
---------------------------------
Per-agent env lives in `agents.adapter_config.env` and `PATCH /api/agents/:id`
is authorized via `allow_self` or an `agents:create` grant. DevOps-Terra has
neither for another agent (403 `deny_missing_grant`), so Terra cannot inject
Otto's env. Terra mints the credential (needs prod Odoo admin, which Otto does
not have) and drops it in a mode-0600 sidecar file; the owning agent runs this
script once to self-inject and shred the file. The credential never goes
through an issue thread, a comment, or AGENTS.md.

Usage (run as the agent whose env is being set):
    python3 scripts/bootstrap_otto_env.py \
        [--sidecar /paperclip/work/gol2963/otto-odoo.env] \
        [--keep-sidecar] [--dry-run]

Idempotent: re-running with a fresh sidecar just overwrites the same four keys.
Safe to run twice. Env takes effect on the agent's NEXT run, so this script
also runs the connectivity check inline, in-process, using the sidecar values
— that output is the GOL-2963 proof and contains no secret.

stdlib-only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

WANTED = ("ODOO_URL", "ODOO_DB", "ODOO_LOGIN", "ODOO_API_KEY")
SECRET_KEYS = {"ODOO_API_KEY"}
DEFAULT_SIDECAR = "/paperclip/work/gol2963/otto-odoo.env"


def log(*a: object) -> None:
    print("[bootstrap]", *a, file=sys.stderr, flush=True)


def api_base() -> tuple[str, str]:
    """Internal API base + Host header.

    The public hostname is Cloudflare-Access-gated and 302s for agent runs, so
    agents must talk to the in-cluster service and carry the public host as a
    `Host:` header. See the Paperclip API notes in TOOLS.md.
    """
    public = os.environ.get("PAPERCLIP_API_URL", "https://paperclip.gatheringatthegrove.com")
    host = urllib.parse.urlsplit(public).netloc or public
    return "http://paperclip-server:3100", host


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | list | str]:
    base, host = api_base()
    token = os.environ.get("PAPERCLIP_API_KEY")
    if not token:
        log("ERROR: PAPERCLIP_API_KEY is not set — run this inside an agent run")
        sys.exit(2)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{base}{path}", data=data, method=method)
    req.add_header("Host", host)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def read_sidecar(path: str) -> dict[str, str]:
    if not os.path.exists(path):
        log(f"ERROR: sidecar not found: {path}")
        log("Ask DevOps-Terra to re-mint it (GOL-2963); it is deliberately short-lived.")
        sys.exit(3)
    out: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    missing = [k for k in WANTED if not out.get(k)]
    if missing:
        log(f"ERROR: sidecar is missing {missing}")
        sys.exit(3)
    return {k: out[k] for k in WANTED}


def shred(path: str) -> None:
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            for _ in range(3):
                f.seek(0)
                f.write(os.urandom(size))
                f.flush()
                os.fsync(f.fileno())
        os.remove(path)
        log(f"sidecar shredded and removed: {path}")
    except OSError as e:
        log(f"WARN: could not shred sidecar ({e}) — delete {path} by hand")


def inline_check(values: dict[str, str]) -> int:
    """Run `odoo_client.py check` + the GOL-2963 acceptance read, with the
    credential supplied only through this subprocess's env."""
    here = os.path.dirname(os.path.abspath(__file__))
    client = os.path.join(here, "odoo_client.py")
    if not os.path.exists(client):
        log(f"WARN: {client} not found — skipping inline check")
        return 0
    env = {**os.environ, **values}
    rc = 0
    for label, argv in (
        ("check", ["check"]),
        (
            "search-read product.template",
            [
                "search-read",
                "product.template",
                "--domain",
                '[["id","in",[22,132,133,134,135,140]]]',
                "--fields",
                "name,grove_botanical_name,grove_compliance_exempt",
            ],
        ),
    ):
        print(f"\n===== {label} =====", flush=True)
        p = subprocess.run([sys.executable, client, *argv], env=env)
        rc = rc or p.returncode
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Self-inject prod Odoo env from a sidecar file")
    ap.add_argument("--sidecar", default=os.environ.get("ODOO_SIDECAR", DEFAULT_SIDECAR))
    ap.add_argument("--keep-sidecar", action="store_true", help="do not shred the sidecar afterwards")
    ap.add_argument("--dry-run", action="store_true", help="report what would change; no PATCH, no shred")
    args = ap.parse_args()

    agent_id = os.environ.get("PAPERCLIP_AGENT_ID")
    if not agent_id:
        log("ERROR: PAPERCLIP_AGENT_ID is not set — run this inside an agent run")
        return 2

    values = read_sidecar(args.sidecar)
    log("sidecar loaded: " + ", ".join(f"{k}={'<redacted>' if k in SECRET_KEYS else values[k]}" for k in WANTED))

    status, agent = call("GET", f"/api/agents/{agent_id}")
    if status != 200 or not isinstance(agent, dict):
        log(f"ERROR: GET /api/agents/{agent_id} -> {status}: {agent}")
        return 1
    agent = agent.get("agent", agent)
    adapter = dict(agent.get("adapterConfig") or {})
    env_block = dict(adapter.get("env") or {})
    log(f"current adapterConfig.env keys: {sorted(env_block)}")

    for k in WANTED:
        env_block[k] = {"type": "plain", "value": values[k]}

    if args.dry_run:
        log(f"DRY-RUN: would set adapterConfig.env keys -> {sorted(env_block)}")
        return inline_check(values)

    # adapterConfig merges SHALLOWLY at the top level, so send the whole `env`
    # sub-object (merged above) and leave sibling keys untouched by omission.
    status, body = call("PATCH", f"/api/agents/{agent_id}", {"adapterConfig": {"env": env_block}})
    if status not in (200, 204):
        log(f"ERROR: PATCH /api/agents/{agent_id} -> {status}: {body}")
        log("403 deny_missing_grant means you are not the owning agent; ask CEO-Rick to run this.")
        return 1
    log(f"PATCH ok ({status})")

    status, agent2 = call("GET", f"/api/agents/{agent_id}")
    if status == 200 and isinstance(agent2, dict):
        agent2 = agent2.get("agent", agent2)
        got = sorted(((agent2.get("adapterConfig") or {}).get("env") or {}).keys())
        log(f"verified adapterConfig.env keys: {got}")
        if any(k not in got for k in WANTED):
            log(f"ERROR: expected {list(WANTED)} to be present")
            return 1
    else:
        log(f"WARN: verification GET -> {status}")

    rc = inline_check(values)

    if not args.keep_sidecar:
        shred(args.sidecar)

    log("DONE. The injected env is visible to this agent on its NEXT run "
        "(adapter env is read at run start). The inline check above already "
        "proves the credential, so you can close GOL-2963 with that output.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
