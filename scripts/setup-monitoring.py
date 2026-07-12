#!/usr/bin/env python3
"""
Bootstrap the Grove monitoring stack — OpenObserve + Keep.

Remediated (GOL-278) to the REAL, empirically-verified API contracts of the
deployed OpenObserve v0.91.1 and Keep 0.54.1 (the previous version was authored
against assumed shapes and 404'd/500'd on every upload). Contracts confirmed by
probing the live grove-obs droplet:

  OpenObserve v0.91.1 (Basic auth, root creds)
    - Alerts are v2:  POST /api/v2/{org}/alerts   (v1 /api/{org}/alerts is 404)
      v2 alerts reference a NAMED destination + template, not an inline URL.
    - Destinations:   POST /api/{org}/alerts/destinations   (name, url, method,
      template, headers, type). NB: OO has an SSRF guard that REJECTS private-IP
      destination URLs — the Keep endpoint must be an SSRF-allowed address
      (public IP / loopback with ZO_SSRF_ALLOW_LOOPBACK). See KEEP_EVENT_URL.
    - Templates:      POST /api/{org}/alerts/templates       (name, body, type).
    - Dashboards:     POST /api/{org}/dashboards             (v5 schema w/ tabs).
    - There is NO /synthetic endpoint — synthetic monitors are NOT OO objects.
      They configure the external synthetic-runner (Hurl), which ships metrics
      to OO; the synthetic-* alerts fire on those metric streams. monitors.json
      is therefore reference-only and is no longer POSTed here.

  Keep 0.54.1 (API at ROOT — not /api/v1; auth = X-API-KEY header)
    - Providers:  POST /providers/install  with the provider config fields at
      TOP LEVEL (discord -> top-level webhook_url), not nested.
    - Workflows:  POST /workflows  (multipart, field `file`) — ONE singular
      `workflow:` doc per file. keep/workflows/*.yml is one file per workflow.
    - Test event: POST /alerts/event?provider_id=openobserve  -> 202.

Reads the canonical configs in:
    openobserve/keep-destination.json   (OO template + Keep-webhook destination)
    openobserve/alerts.json             (OO v2 alert objects)
    openobserve/dashboards.json         (OO v5 dashboards)
    keep/providers.yml                  (Keep provider install defs)
    keep/workflows/*.yml                (one singular Keep workflow per file)

Idempotent: re-runs upsert by name/id, so this is safe on every bootstrap.

Local vs remote:
    Local `make monitoring-up` (OpenObserve + Keep side-by-side on the host):
        defaults hit localhost. Note the SSRF caveat still applies to the OO
        destination — set KEEP_EVENT_URL to an SSRF-allowed address.
    Remote (run from the agenticos vantage against grove-obs): run this in a
        python:3.12-slim container ON the grove-obs_obs network so both
        services are reachable by service name, e.g.
            OPENOBSERVE_BASE_URL=http://openobserve:5080 \
            KEEP_BASE_URL=http://keep-backend:8080 \
            ./scripts/setup-monitoring.py

Secrets (from .env.monitoring / op at runtime):
    OPENOBSERVE_ROOT_EMAIL / OPENOBSERVE_ROOT_PASSWORD  -> OO Basic auth
    KEEP_WEBHOOK_TOKEN        -> Keep X-API-KEY + OO destination header
    DISCORD_WEBHOOK_WARNING   -> Keep discord-warning provider webhook_url
    DISCORD_WEBHOOK_CRITICAL  -> Keep discord-critical provider webhook_url

Usage:
    ./scripts/setup-monitoring.py            # reads env from the shell
    make monitoring-setup                    # reads env from .env.monitoring
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request

# ── paths (resolved from this script's location, not CWD) ─────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
OPENOBSERVE_DIR = REPO_ROOT / "openobserve"
KEEP_DIR = REPO_ROOT / "keep"
KEEP_WORKFLOWS_DIR = KEEP_DIR / "workflows"
SYNTHETIC_DIR = REPO_ROOT / "synthetic"

OO_ORG = os.environ.get("OPENOBSERVE_ORG", "default")


def _log(*args: Any) -> None:
    """All output to stderr so callers can pipe stdout if needed."""
    print(*args, file=sys.stderr, flush=True)


# ── env helpers ──────────────────────────────────────────────────────────────
def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        _log(f"ERROR: required env var {name} is not set.")
        _log("  Source it from .env.monitoring or export it before running.")
        sys.exit(1)
    return val


def get_env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# ── HTTP wrapper ─────────────────────────────────────────────────────────────
def http_request(
    method: str,
    url: str,
    *,
    body: Any = None,
    auth: tuple[str, str] | None = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
    content_type: str = "application/json",
    timeout: float = 20.0,
) -> tuple[int, Any]:
    """Minimal stdlib HTTP client. Returns (status_code, parsed_body_or_text)."""
    hdrs = {"Accept": "application/json"}
    if headers:
        hdrs.update(headers)
    if auth:
        import base64

        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        hdrs["Authorization"] = f"Basic {token}"
    if raw is not None:
        data = raw
        hdrs["Content-Type"] = content_type
    elif body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = content_type
    else:
        data = None
    req = request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode()
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                return resp.status, text
    except urlerror.HTTPError as e:
        text = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, text
    except urlerror.URLError as e:
        _log(f"  network error: {e.reason}")
        return 0, str(e.reason)


def wait_for(url: str, max_wait_s: int, *, name: str) -> None:
    """Block until url returns any HTTP code other than 0 (connection error)."""
    _log(f"  waiting for {name} at {url} (up to {max_wait_s}s)...")
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        code, _ = http_request("GET", url, timeout=4)
        if code != 0:
            _log(f"  {name} responding (HTTP {code})")
            return
        time.sleep(2)
    _log(f"  ERROR: {name} never responded within {max_wait_s}s")
    sys.exit(1)


# ── OpenObserve API ──────────────────────────────────────────────────────────
def oo_root() -> str:
    """Scheme+host of OpenObserve (no /api suffix). Used for /healthz."""
    return get_env("OPENOBSERVE_BASE_URL", f"http://localhost:{get_env('OPENOBSERVE_PORT', '5080')}").rstrip("/")


def oo_api() -> str:
    """v1 API base: {root}/api/{org} — templates, destinations, dashboards."""
    return f"{oo_root()}/api/{OO_ORG}"


def oo_v2_api() -> str:
    """v2 API base: {root}/api/v2/{org} — alerts."""
    return f"{oo_root()}/api/v2/{OO_ORG}"


def oo_auth() -> tuple[str, str]:
    return require_env("OPENOBSERVE_ROOT_EMAIL"), require_env("OPENOBSERVE_ROOT_PASSWORD")


def keep_event_url() -> str:
    """
    The URL OpenObserve's alert destination POSTs to reach Keep.

    OpenObserve v0.91.1 has an SSRF guard that rejects private-IP destination
    URLs at CREATE time, so this cannot be the internal keep-backend service
    name / private IP. Set KEEP_EVENT_URL to an SSRF-allowed address that
    reaches Keep — e.g. the obs droplet's PUBLIC IP with keep-backend published
    admin-only + firewalled (hairpin NAT verified working), or a loopback
    address paired with ZO_SSRF_ALLOW_LOOPBACK=true.
    """
    explicit = get_env("KEEP_EVENT_URL", "")
    if explicit:
        return explicit.rstrip("/")
    # No safe default exists (any in-network default trips the SSRF guard).
    return ""


def oo_upsert_template(tmpl: dict) -> None:
    name = tmpl["name"]
    code, resp = http_request("POST", f"{oo_api()}/alerts/templates", body=tmpl, auth=oo_auth())
    if code in (200, 201):
        _log(f"    ✓ template {name}")
        return
    # Update path — OO templates PUT by name.
    code2, resp2 = http_request("PUT", f"{oo_api()}/alerts/templates/{name}", body=tmpl, auth=oo_auth())
    if code2 in (200, 201, 204):
        _log(f"    ✓ template {name} (updated)")
    else:
        _log(f"    ✗ template {name} — POST {code} {resp} / PUT {code2} {resp2}")


def oo_upsert_destination(dest: dict) -> None:
    name = dest["name"]
    if not dest.get("url"):
        _log(f"    ⚠ destination {name} SKIPPED — KEEP_EVENT_URL unset (see docstring re SSRF guard)")
        return
    code, resp = http_request("POST", f"{oo_api()}/alerts/destinations", body=dest, auth=oo_auth())
    if code in (200, 201):
        _log(f"    ✓ destination {name}")
        return
    code2, resp2 = http_request("PUT", f"{oo_api()}/alerts/destinations/{name}", body=dest, auth=oo_auth())
    if code2 in (200, 201, 204):
        _log(f"    ✓ destination {name} (updated)")
    else:
        _log(f"    ✗ destination {name} — POST {code} {resp} / PUT {code2} {resp2}")


def oo_existing_alert_ids() -> dict[str, str]:
    """name -> id map of existing v2 alerts, for upsert-by-name."""
    code, resp = http_request("GET", f"{oo_v2_api()}/alerts", auth=oo_auth())
    out: dict[str, str] = {}
    if code == 200 and isinstance(resp, dict):
        for a in resp.get("list", []):
            alert = a.get("alert", a) if isinstance(a, dict) else {}
            nm = alert.get("name") or a.get("name")
            aid = alert.get("id") or a.get("id") or a.get("alert_id")
            if nm and aid:
                out[nm] = aid
    return out


def oo_bootstrap_alerts(dest_available: bool) -> None:
    src = OPENOBSERVE_DIR / "alerts.json"
    data = json.loads(src.read_text())
    alerts = [a for a in data.get("alerts", []) if not a.get("_comment")]
    _log(f"  uploading {len(alerts)} v2 alert rules...")
    if not dest_available:
        _log("  NOTE: KEEP_EVENT_URL unset — alerts still upsert but will fail to notify")
        _log("        until the Keep destination exists. See docstring (SSRF guard).")
    existing = oo_existing_alert_ids()
    ok = 0
    for a in alerts:
        payload = {k: v for k, v in a.items() if not k.startswith("_")}
        name = payload["name"]
        if name in existing:
            code, resp = http_request(
                "PUT", f"{oo_v2_api()}/alerts/{existing[name]}", body=payload, auth=oo_auth()
            )
            verb = "updated"
        else:
            code, resp = http_request("POST", f"{oo_v2_api()}/alerts", body=payload, auth=oo_auth())
            verb = "created"
        if code in (200, 201):
            ok += 1
            _log(f"    ✓ {name} ({verb})")
        else:
            _log(f"    ✗ {name} — HTTP {code} {str(resp)[:160]}")
    _log(f"  alerts: {ok}/{len(alerts)} upserted")


def oo_existing_dashboards() -> dict[str, tuple[str, str]]:
    """title -> (dashboardId, hash). OO's dashboard PUT requires the current hash
    (concurrency guard) — a PUT without it 500s ('missing or invalid hash')."""
    code, resp = http_request("GET", f"{oo_api()}/dashboards", auth=oo_auth())
    out: dict[str, tuple[str, str]] = {}
    if code == 200 and isinstance(resp, dict):
        for d in resp.get("dashboards", []):
            h = str(d.get("hash", ""))
            for vk in ("v5", "v4", "v3", "v2", "v1"):
                v = d.get(vk)
                if v and v.get("title"):
                    out[v["title"]] = (v.get("dashboardId", ""), h)
    return out


def oo_bootstrap_dashboards() -> None:
    src = OPENOBSERVE_DIR / "dashboards.json"
    data = json.loads(src.read_text())
    dashboards = [d for d in data.get("dashboards", []) if not d.get("_comment")]
    _log(f"  uploading {len(dashboards)} dashboards...")
    existing = oo_existing_dashboards()
    for d in dashboards:
        payload = {k: v for k, v in d.items() if not k.startswith("_")}
        title = payload.get("title", "")
        if title in existing and existing[title][0]:
            did, h = existing[title]
            url = f"{oo_api()}/dashboards/{did}"
            if h:
                url += f"?hash={h}"
            code, resp = http_request("PUT", url, body=payload, auth=oo_auth())
            verb = "updated"
        else:
            code, resp = http_request("POST", f"{oo_api()}/dashboards", body=payload, auth=oo_auth())
            verb = "created"
        status = "✓" if code in (200, 201) else f"✗ HTTP {code} {str(resp)[:120]}"
        _log(f"    {status} {title} ({verb})")


def oo_bootstrap() -> dict:
    """Create template + destination, return the destination dict (for reuse)."""
    src = OPENOBSERVE_DIR / "keep-destination.json"
    cfg = json.loads(src.read_text())
    tmpl = cfg["template"]
    dest = dict(cfg["destination"])
    # Substitute the live secret + resolved Keep URL into the destination.
    dest["url"] = keep_event_url()
    dest.setdefault("headers", {})["X-API-KEY"] = require_env("KEEP_WEBHOOK_TOKEN")
    _log("  upserting OO alert template + Keep-webhook destination...")
    oo_upsert_template(tmpl)
    oo_upsert_destination(dest)
    return dest


# ── Keep API ─────────────────────────────────────────────────────────────────
def keep_base() -> str:
    """Keep API root (NO /api/v1 in 0.54.1)."""
    return get_env("KEEP_BASE_URL", f"http://localhost:{get_env('KEEP_BACKEND_PORT', '8080')}").rstrip("/")


def keep_headers() -> dict[str, str]:
    return {"X-API-KEY": require_env("KEEP_WEBHOOK_TOKEN")}


def keep_bootstrap_providers() -> None:
    """
    Install Keep providers. Config fields go at the TOP LEVEL of the install
    body (discord -> top-level webhook_url), not nested under provider_config.
    """
    src = KEEP_DIR / "providers.yml"
    raw = src.read_text()
    # Minimal flat-YAML reader: each provider is a block of scalar fields.
    providers: list[dict[str, str]] = []
    cur: dict[str, str] | None = None
    for line in raw.splitlines():
        m = re.match(r"^\s*-\s+provider_id:\s+(\S+)", line)
        if m:
            if cur:
                providers.append(cur)
            cur = {"provider_id": m.group(1)}
            continue
        if cur is None:
            continue
        m = re.match(r"^\s+(\w+):\s+\"?(.+?)\"?\s*$", line)
        if m:
            cur[m.group(1)] = m.group(2)
    if cur:
        providers.append(cur)

    _log(f"  installing {len(providers)} Keep providers...")
    for p in providers:
        webhook_url = require_env(p["webhook_url_env"]) if p.get("webhook_url_env") else p.get("webhook_url", "")
        install_body = {
            "provider_id": p["provider_id"],
            "provider_name": p.get("provider_name", p["provider_id"]),
            "provider_type": p["provider_type"],
            # config fields at TOP LEVEL (verified) — NOT nested
            "webhook_url": webhook_url,
        }
        code, resp = http_request(
            "POST", f"{keep_base()}/providers/install", body=install_body, headers=keep_headers()
        )
        if code in (200, 201):
            _log(f"    ✓ {p['provider_id']}")
        elif code in (409, 412) or (isinstance(resp, (dict, str)) and "already" in str(resp).lower()):
            _log(f"    ✓ {p['provider_id']} (already installed)")
        else:
            _log(f"    ✗ {p['provider_id']} — HTTP {code} {str(resp)[:160]}")


def _multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----grove{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: application/x-yaml\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _keep_workflows_by_name() -> dict[str, list[str]]:
    """name -> [workflow uuids]. Keep assigns its own UUID per workflow; the YAML
    `id`/`name` is not a natural key, so re-POST creates duplicates. We key on the
    `name` field to converge to exactly one workflow per managed file."""
    code, resp = http_request("GET", f"{keep_base()}/workflows", headers=keep_headers())
    out: dict[str, list[str]] = {}
    if code == 200 and isinstance(resp, list):
        for w in resp:
            nm, wid = w.get("name"), w.get("id")
            if nm and wid:
                out.setdefault(nm, []).append(wid)
    return out


def _workflow_name(text: str) -> str:
    m = re.search(r"^\s*name:\s*\"?(.+?)\"?\s*$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def keep_bootstrap_workflows() -> None:
    """
    Upload each keep/workflows/*.yml as a SINGULAR `workflow:` doc via multipart
    (field `file`). Keep expects one workflow per file.

    Idempotent by NAME: Keep's POST /workflows always creates a new workflow (it
    does not upsert by the YAML id), so we delete any existing workflow with the
    same `name` first, then create — converging to exactly one per file.
    """
    files = sorted(KEEP_WORKFLOWS_DIR.glob("*.yml")) if KEEP_WORKFLOWS_DIR.is_dir() else []
    if not files:
        _log("  no keep/workflows/*.yml found — skipping")
        return
    _log(f"  uploading {len(files)} Keep workflows (multipart, singular docs)...")
    existing = _keep_workflows_by_name()
    for f in files:
        text = f.read_text()
        # Inject secrets: replace __ENVVAR__ placeholders with their env values.
        # Workflows configure the discord webhook INLINE (Keep 0.54.1 installed-
        # provider references are unreliable — UUID ids + soft-delete name residue),
        # so the webhook is substituted here at upload time; the repo keeps only the
        # placeholder.
        for tok in re.findall(r"__([A-Z0-9_]+)__", text):
            val = os.environ.get(tok)
            if val:
                text = text.replace(f"__{tok}__", val)
            else:
                _log(f"    ⚠ {f.name}: env {tok} unset — placeholder left unresolved")
        raw = text.encode()
        name = _workflow_name(text)
        for wid in existing.get(name, []):
            http_request("DELETE", f"{keep_base()}/workflows/{wid}", headers=keep_headers())
        body, ctype = _multipart("file", f.name, raw)
        code, resp = http_request(
            "POST", f"{keep_base()}/workflows", raw=body, content_type=ctype, headers=keep_headers()
        )
        if code in (200, 201):
            _log(f"    ✓ {f.name} ({name})")
        else:
            _log(f"    ✗ {f.name} — HTTP {code} {str(resp)[:160]}")


# ── synthetic canary (opt-in) ─────────────────────────────────────────────────
def maybe_seed_canary() -> None:
    if get_env("SYNTHETIC_CANARY_ENABLED", "false").strip().lower() != "true":
        _log("  checkout-canary disabled (SYNTHETIC_CANARY_ENABLED!=true) — skipping seed")
        return
    canary = SYNTHETIC_DIR / "canary.py"
    if not canary.exists():
        _log("  canary.py not present — skipping seed")
        return
    rc = subprocess.run([sys.executable, str(canary), "--seed"], check=False).returncode
    _log("  ✓ canary product seeded" if rc == 0 else f"  ⚠ canary seed failed (rc={rc})")


# ── main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    _log("=== Grove monitoring bootstrap (OpenObserve v0.91.1 / Keep 0.54.1) ===\n")

    _log("Step 1/5: wait for OpenObserve")
    wait_for(f"{oo_root()}/healthz", 90, name="OpenObserve")

    _log("\nStep 2/5: wait for Keep backend")
    wait_for(f"{keep_base()}/healthcheck", 120, name="Keep")

    _log("\nStep 3/5: bootstrap OpenObserve (template + destination + alerts + dashboards)")
    maybe_seed_canary()
    dest = oo_bootstrap()
    oo_bootstrap_alerts(dest_available=bool(dest.get("url")))
    oo_bootstrap_dashboards()

    _log("\nStep 4/5: bootstrap Keep (providers + workflows)")
    keep_bootstrap_providers()
    keep_bootstrap_workflows()

    _log("\nStep 5/5: summary")
    _log("=" * 60)
    _log(f"  OpenObserve:  {oo_root()}")
    _log(f"  Keep:         {keep_base()}")
    if not dest.get("url"):
        _log("  ⚠ KEEP_EVENT_URL unset — OO alerts will NOT reach Keep until the")
        _log("    Keep destination is created against an SSRF-allowed URL (see docstring).")
    _log("")
    _log("Verify Discord routing (fires a test event through Keep → Discord):")
    _log("  curl -X POST '%s/alerts/event?provider_id=openobserve' \\" % keep_base())
    _log("    -H \"X-API-KEY: $KEEP_WEBHOOK_TOKEN\" -H 'Content-Type: application/json' \\")
    _log('    -d \'{"name":"connectivity-check","severity":"warning","message":"hello"}\'')
    _log("  → appears in #grove-alerts-warning within a few seconds (202 Accepted)")


if __name__ == "__main__":
    main()
