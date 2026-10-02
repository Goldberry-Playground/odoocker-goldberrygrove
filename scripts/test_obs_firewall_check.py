#!/usr/bin/env python3
"""Regression tests for the grove-obs-fw drift check (GOL-2333 / GOL-2631).

Guards the invariant that makes this check safe to cron: a BROKEN check must
never be able to impersonate DRIFT. `.github/workflows/obs-firewall-drift.yml`
retries exit 2 ("check broken", incl. a transient DO API failure) but never
retries exit 1 ("drift") -- so if a renamed or default-less variable aborted the
script with a bare exit 1, the nightly watcher would page about firewall drift
that does not exist, and keep doing so every night (GOL-2564).

It also pins SOURCE PRECEDENCE. The script reads this env's HCL rather than
running `terraform plan` (the env declares ~20 mostly-secret vars, so a plan
cannot be cronned honestly). Reading only the variables.tf defaults would
compare live DO against values that were never applied -- an explicit
terraform.tfvars assignment has to win, exactly as Terraform resolves it.

No network, no DigitalOcean, no Terraform. The REAL script is executed via a
symlink (so HERE/variables.tf points into a fixture dir) with `curl` replaced by
a stub on PATH, so what is asserted is the actual verdict the script reaches:

  renamed-var-is-exit-2   a variable this script mirrors going missing from
                          variables.tf is exit 2 WITH a diagnostic, never the
                          silent bare exit 1 that reads as drift.
  no-default-is-exit-2    variable present but default-less and unset in tfvars
                          is "drift unknowable" (exit 2), not clean, not drift.
  empty-admin-is-exit-2   an admin allowlist that resolves empty is refused --
                          a firewall with no admin access is never "OK".
  tfvars-beats-default    tfvars admin_ip_cidrs is what gets compared; live
                          matching tfvars is clean even though it differs from
                          the variables.tf default.
  default-when-no-tfvars  with no tfvars the codified default is compared.
  empty-tfvars-list-real  `ingest_source_cidrs = []` is a real "admin-only"
                          value, not "unset" -- a live ingest CIDR is then
                          UNEXPECTED drift.
  tags-are-compared       ingest_source_tags is a live rule on 5080
                          (main.tf source_tags); a missing tag IS drift.
  clean-is-exit-0         a fully matching firewall exits 0.
  missing-token-is-exit-2 no DO token is bad env, not drift.

    python3 scripts/test_obs_firewall_check.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_SCRIPT = os.path.join(
    _REPO, "infra", "terraform", "environments", "observability", "scripts", "check-firewall.sh"
)

ADMIN_DEFAULT = ["74.47.41.38/32", "173.84.140.152/32"]
ADMIN_TFVARS = ["203.0.113.7/32"]
AUTOMATION = ["159.223.171.231/32"]
INGEST = ["159.223.171.231/32"]
TAGS = ["role-odoo", "env-qa-l3"]
CF = ["173.245.48.0/20", "2400:cb00::/32"]


def _hcl_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


def variables_tf(
    *,
    admin: list[str] | None = ADMIN_DEFAULT,
    admin_declared: bool = True,
    automation: list[str] | None = AUTOMATION,
    ingest: list[str] | None = INGEST,
    tags: list[str] | None = TAGS,
    cf: list[str] | None = CF,
) -> str:
    """A fixture variables.tf. `None` = variable declared with NO default."""
    out = []
    spec = [
        ("admin_ip_cidrs", admin, admin_declared),
        ("automation_ssh_cidrs", automation, True),
        ("ingest_source_cidrs", ingest, True),
        ("ingest_source_tags", tags, True),
        ("cloudflare_ingress_cidrs", cf, True),
    ]
    for name, values, declared in spec:
        if not declared:
            continue
        out.append(f'variable "{name}" {{')
        # A description that says the word "default" must not be mistaken for
        # the assignment -- that anchoring is part of what is under test.
        out.append(f'  description = "fixture for {name}; the default below is authoritative."')
        out.append("  type        = list(string)")
        if values is not None:
            out.append(f"  # codified (GOL-2333)\n  default = {_hcl_list(values)}")
        out.append("}\n")
    return "\n".join(out)


def firewall_json(
    *,
    admin: list[str],
    automation: list[str],
    ingest: list[str],
    tags: list[str],
    cf: list[str],
    extra_rules: list[dict] | None = None,
) -> dict:
    """Shape of GET /v2/firewalls, mirroring main.tf's rule split."""
    rules = [
        {"ports": "22", "sources": {"addresses": admin + automation}},
        {"ports": "5080", "sources": {"addresses": admin + ingest}},
        {"ports": "5080", "sources": {"tags": tags}},
        {"ports": "3034", "sources": {"addresses": admin}},
        {"ports": "8080", "sources": {"addresses": admin}},
        {"ports": "443", "sources": {"addresses": cf}},
    ]
    rules += extra_rules or []
    return {
        "firewalls": [
            {
                "name": "grove-obs-fw",
                "status": "succeeded",
                "droplet_ids": [12345],
                "inbound_rules": rules,
            }
        ]
    }


CURL_STUB = """#!/usr/bin/env bash
# Stand-in for the DO API. The real script calls `curl ... /v2/<path>`.
url="${@: -1}"
case "$url" in
  */firewalls*) cat "$STUB_FW_JSON" ;;
  */tags/*)     printf '{"tag":{"resources":{"droplets":{"count":%s}}}}' "${STUB_TAG_COUNT:-1}" ;;
  *) echo "unexpected url: $url" >&2; exit 22 ;;
esac
"""


def run(
    *,
    variables: str,
    fw: dict | None = None,
    tfvars: str | None = None,
    token: str | None = "ro-token",
    tag_count: str = "1",
) -> subprocess.CompletedProcess:
    tmp = tempfile.mkdtemp(prefix="obsfw-")
    try:
        envdir = os.path.join(tmp, "env")
        os.makedirs(os.path.join(envdir, "scripts"))
        with open(os.path.join(envdir, "variables.tf"), "w") as fh:
            fh.write(variables)
        if tfvars is not None:
            with open(os.path.join(envdir, "terraform.tfvars"), "w") as fh:
                fh.write(tfvars)
        # Symlink, not a copy: the REAL script is what runs, and HERE/..
        # resolves into the fixture env dir.
        os.symlink(_SCRIPT, os.path.join(envdir, "scripts", "check-firewall.sh"))

        bindir = os.path.join(tmp, "bin")
        os.makedirs(bindir)
        curl = os.path.join(bindir, "curl")
        with open(curl, "w") as fh:
            fh.write(CURL_STUB)
        os.chmod(curl, 0o755)

        fwpath = os.path.join(tmp, "fw.json")
        with open(fwpath, "w") as fh:
            json.dump(fw if fw is not None else {"firewalls": []}, fh)

        env = {
            "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
            "STUB_FW_JSON": fwpath,
            "STUB_TAG_COUNT": tag_count,
            "HOME": tmp,
        }
        if token is not None:
            env["DO_TOKEN"] = token
        return subprocess.run(
            ["bash", os.path.join(envdir, "scripts", "check-firewall.sh")],
            capture_output=True,
            text=True,
            env=env,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else f"\n         {detail}"))
    if not cond:
        FAILURES.append(name)


def main() -> int:
    print(f"check-firewall.sh regression tests\n  script: {_SCRIPT}\n")

    clean_fw = firewall_json(
        admin=ADMIN_DEFAULT, automation=AUTOMATION, ingest=INGEST, tags=TAGS, cf=CF
    )

    # ---- the headline: a broken check must not read as drift ----------------
    print("renamed-var-is-exit-2")
    r = run(variables=variables_tf(admin_declared=False), fw=clean_fw)
    check("exit 2, not 1", r.returncode == 2, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    check(
        "names the missing variable",
        "admin_ip_cidrs" in r.stderr and "declares no variable" in r.stderr,
        f"stderr={r.stderr!r}",
    )
    check("not silent", bool(r.stderr.strip()), "produced no diagnostic at all")

    print("no-default-is-exit-2")
    r = run(variables=variables_tf(admin=None), fw=clean_fw)
    check("exit 2, not 1", r.returncode == 2, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    check(
        "says drift is unknowable",
        "no default" in r.stderr and "unknowable" in r.stderr,
        f"stderr={r.stderr!r}",
    )

    print("empty-admin-is-exit-2")
    r = run(variables=variables_tf(admin=[]), fw=clean_fw)
    check("exit 2, not 0", r.returncode == 2, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    check("refuses to grade it clean", "EMPTY" in r.stderr, f"stderr={r.stderr!r}")

    # ---- source precedence --------------------------------------------------
    print("tfvars-beats-default")
    fw_tfvars = firewall_json(
        admin=ADMIN_TFVARS, automation=AUTOMATION, ingest=INGEST, tags=TAGS, cf=CF
    )
    r = run(
        variables=variables_tf(),
        tfvars=f"admin_ip_cidrs = {_hcl_list(ADMIN_TFVARS)}\n",
        fw=fw_tfvars,
    )
    check(
        "live==tfvars is clean (exit 0)",
        r.returncode == 0,
        f"rc={r.returncode}\n{r.stdout}{r.stderr}",
    )
    check(
        "provenance says tfvars",
        "admin<-terraform.tfvars" in r.stdout,
        f"stdout={r.stdout!r}",
    )
    check(
        "warns the default is shadowed",
        "shadowing the variables.tf default" in r.stdout,
        f"stdout={r.stdout!r}",
    )
    # ...and the same tree WITHOUT the tfvars must now see that firewall as drift.
    r2 = run(variables=variables_tf(), fw=fw_tfvars)
    check(
        "same live fw is DRIFT against the default alone",
        r2.returncode == 1 and "DRIFT" in r2.stdout,
        f"rc={r2.returncode}\n{r2.stdout}",
    )

    print("default-when-no-tfvars")
    r = run(variables=variables_tf(), fw=clean_fw)
    check("exit 0", r.returncode == 0, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    check(
        "provenance says variables.tf",
        "admin<-variables.tf default" in r.stdout,
        f"stdout={r.stdout!r}",
    )
    check("no shadow warning", "shadowing" not in r.stdout, f"stdout={r.stdout!r}")

    print("empty-tfvars-list-real")
    # `ingest_source_cidrs = []` is "admin-only", NOT "fall back to the default".
    r = run(
        variables=variables_tf(),
        tfvars="ingest_source_cidrs = []\n",
        fw=clean_fw,  # still carries the ingest CIDR on 5080
    )
    check("exit 1 (drift)", r.returncode == 1, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    check(
        "flags the live ingest CIDR as UNEXPECTED on 5080",
        f"DRIFT :5080 UNEXPECTED address {INGEST[0]}" in r.stdout,
        f"stdout={r.stdout!r}",
    )

    # ---- tags really are part of the contract (main.tf source_tags) ---------
    print("tags-are-compared")
    fw_missing_tag = firewall_json(
        admin=ADMIN_DEFAULT, automation=AUTOMATION, ingest=INGEST, tags=["role-odoo"], cf=CF
    )
    r = run(variables=variables_tf(), fw=fw_missing_tag)
    check("exit 1 (drift)", r.returncode == 1, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    check(
        "names the missing tag",
        "DRIFT :5080 MISSING tag env-qa-l3" in r.stdout,
        f"stdout={r.stdout!r}",
    )
    # A tag matching zero droplets is a note, not drift (immutable rebuilds).
    r = run(variables=variables_tf(), fw=clean_fw, tag_count="0")
    check(
        "zero-droplet tag is a note, not drift",
        r.returncode == 0 and "matches 0 droplet(s)" in r.stdout,
        f"rc={r.returncode}\n{r.stdout}",
    )

    # ---- baseline ----------------------------------------------------------
    print("clean-is-exit-0")
    r = run(variables=variables_tf(), fw=clean_fw)
    check(
        "exit 0 and says OK",
        r.returncode == 0 and "OK: live grove-obs-fw" in r.stdout,
        f"rc={r.returncode}\n{r.stdout}{r.stderr}",
    )

    print("undeclared-port-is-drift")
    fw_extra = firewall_json(
        admin=ADMIN_DEFAULT,
        automation=AUTOMATION,
        ingest=INGEST,
        tags=TAGS,
        cf=CF,
        extra_rules=[{"ports": "9999", "sources": {"addresses": ["0.0.0.0/0"]}}],
    )
    r = run(variables=variables_tf(), fw=fw_extra)
    check(
        "exit 1 and names the undeclared port",
        r.returncode == 1 and "DRIFT :9999 UNDECLARED" in r.stdout,
        f"rc={r.returncode}\n{r.stdout}",
    )

    print("missing-token-is-exit-2")
    r = run(variables=variables_tf(), fw=clean_fw, token=None)
    check("exit 2", r.returncode == 2, f"rc={r.returncode}\n{r.stdout}{r.stderr}")
    check("explains why", "No DO API token" in r.stderr, f"stderr={r.stderr!r}")

    print("absent-firewall-is-exit-2")
    r = run(variables=variables_tf(), fw={"firewalls": []})
    check(
        "exit 2, not drift",
        r.returncode == 2 and "no DO firewall named" in r.stderr,
        f"rc={r.returncode}\n{r.stderr}",
    )

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
