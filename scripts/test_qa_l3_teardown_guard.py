#!/usr/bin/env python3
"""Regression tests for the grove-qa-l3-obs teardown exemption (GOL-2472).

Guards the invariant that made the first-ever release-train teardown a hard
blocker: `scripts/qa-l3-teardown.sh compute` carried an unconditional
`-target=digitalocean_droplet.obs`, so `make train-teardown` would have
destroyed the exempt QA obs droplet, its firewall and the oo/keep DNS records
(GOL-2333 / docs/ADR/010).

No network, no DigitalOcean, no 1Password. The REAL script is executed with
`op` and `terraform` replaced by stubs on PATH, so what is asserted is the
actual destroy command line the script would have issued:

  exempt-by-default      `compute` with no env var MUST NOT pass
                         -target=digitalocean_droplet.obs, and MUST still
                         target the 4 apps + the Odoo droplet + caddy_data.
  opt-in-restores        QA_L3_TEARDOWN_OBS=1 puts obs back in the targets.
  post-check-passes      when obs + firewall + oo/keep survive in state the
                         script exits 0 and says so.
  post-check-fails-loud  if obs vanished from state anyway (a -target
                         DEPENDENT pulled it in), the script exits non-zero
                         rather than reporting a clean teardown.
  unreadable-state       a state read that errors is "UNVERIFIED", not
                         "destroyed" -- it must not be silently green.
  wrong-confirmation     the typed-confirm gate still refuses.

    python3 scripts/test_qa_l3_teardown_guard.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(_HERE, "qa-l3-teardown.sh")

# What `terraform state list` reports after a healthy `compute` teardown: the
# exempt obs resources plus the surviving data/DNS plane.
_STATE_OBS_ALIVE = "\n".join([
    "digitalocean_database_cluster.pg",
    "digitalocean_droplet.obs",
    "digitalocean_firewall.obs",
    "digitalocean_record.keep",
    "digitalocean_record.oo",
    "digitalocean_reserved_ip.odoo",
    "digitalocean_volume.odoo_filestore",
])
# The failure this whole issue exists to catch: obs gone after teardown.
_STATE_OBS_GONE = "\n".join(
    ln for ln in _STATE_OBS_ALIVE.split("\n")
    if ln not in ("digitalocean_droplet.obs", "digitalocean_firewall.obs")
)

_OP_STUB = """#!/usr/bin/env bash
# fake `op`: drop everything up to `--` and run the rest. No vault, no secrets.
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--" ]; then shift; break; fi
  shift
done
exec "$@"
"""

_TERRAFORM_STUB = """#!/usr/bin/env bash
# fake `terraform`: record the argv, answer `state list` from $FAKE_STATE.
printf '%s\\n' "$*" >> "$TF_ARGV_LOG"
for a in "$@"; do
  if [ "$a" = "list" ]; then
    if [ "${FAKE_STATE_RC:-0}" != "0" ]; then
      echo "Error: Failed to load state" >&2
      exit "$FAKE_STATE_RC"
    fi
    cat "$FAKE_STATE"
    exit 0
  fi
done
exit 0
"""


def _run(mode="compute", confirm=None, env_extra=None, state=_STATE_OBS_ALIVE,
         state_rc=0):
    """Run the real teardown script against the stubs; return (rc, out, argv)."""
    tmp = tempfile.mkdtemp(prefix="qa-l3-teardown-test-")
    for name, body in (("op", _OP_STUB), ("terraform", _TERRAFORM_STUB)):
        path = os.path.join(tmp, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    argv_log = os.path.join(tmp, "argv.log")
    state_file = os.path.join(tmp, "state.txt")
    with open(state_file, "w") as fh:
        fh.write(state + "\n")

    env = dict(os.environ)
    env["PATH"] = tmp + os.pathsep + env["PATH"]
    env["TF_ARGV_LOG"] = argv_log
    env["FAKE_STATE"] = state_file
    env["FAKE_STATE_RC"] = str(state_rc)
    # Normally injected by `op run` from .env.op; the inner shell runs under
    # `set -u`, so it must exist even with the vault stubbed out.
    env["TF_VAR_do_token"] = "dop_v1_stub"
    env.pop("QA_L3_TEARDOWN_OBS", None)
    env.update(env_extra or {})

    if confirm is None:
        confirm = f"destroy-qa-l3-{mode}"
    proc = subprocess.run(
        ["bash", _SCRIPT, mode],
        input=confirm + "\n", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    argv = ""
    if os.path.exists(argv_log):
        with open(argv_log) as fh:
            argv = fh.read()
    return proc.returncode, proc.stdout, argv


def _destroy_line(argv):
    """The single `terraform destroy` invocation from the recorded argv log."""
    lines = [ln for ln in argv.split("\n") if " destroy " in f" {ln} "]
    assert len(lines) == 1, f"expected exactly one destroy call, got: {lines!r}"
    return lines[0]


def test_exempt_by_default():
    rc, out, argv = _run()
    assert rc == 0, f"expected clean exit, got {rc}\n{out}"
    line = _destroy_line(argv)
    assert "-target=digitalocean_droplet.obs" not in line, (
        "GOL-2472 REGRESSION: teardown would destroy the exempt obs droplet:\n"
        + line
    )
    # ...while still tearing down everything it is supposed to.
    for expected in (
        "-target=digitalocean_app.hub",
        "-target=digitalocean_app.tenant",
        "-target=digitalocean_volume_attachment.caddy_data",
        "-target=digitalocean_droplet.odoo",
    ):
        assert expected in line, f"teardown lost a real target {expected}:\n{line}"
    assert "EXEMPT" in out, "operator was not told obs is exempt"


def test_opt_in_restores_obs():
    rc, out, argv = _run(env_extra={"QA_L3_TEARDOWN_OBS": "1"},
                         state=_STATE_OBS_GONE)
    assert rc == 0, f"expected clean exit, got {rc}\n{out}"
    line = _destroy_line(argv)
    assert "-target=digitalocean_droplet.obs" in line, (
        "QA_L3_TEARDOWN_OBS=1 did not opt the obs droplet back in:\n" + line
    )
    # Opted in, obs is *expected* gone -- the post-check must stay quiet.
    assert "FATAL" not in out


def test_post_check_confirms_survivors():
    rc, out, _ = _run()
    assert rc == 0
    assert "Exemption OK" in out, f"no post-teardown proof obs survived:\n{out}"


def test_post_check_fails_loud_when_obs_destroyed():
    rc, out, _ = _run(state=_STATE_OBS_GONE)
    assert rc != 0, "obs was destroyed but the script reported success"
    assert "digitalocean_droplet.obs" in out and "FATAL" in out, out
    assert "Exemption OK" not in out


def test_unreadable_state_is_unverified_not_green():
    rc, out, _ = _run(state_rc=7)
    assert rc != 0, "unreadable state must not pass as a verified exemption"
    assert "UNVERIFIED" in out, out
    assert "Exemption OK" not in out


def test_wrong_confirmation_aborts():
    rc, out, argv = _run(confirm="yes")
    assert rc != 0 and "aborted" in out, out
    assert " destroy " not in f" {argv} ", "destroy ran without the typed confirm"


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {fn.__name__}: {exc}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
