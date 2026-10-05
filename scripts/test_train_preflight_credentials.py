#!/usr/bin/env python3
"""Behavioural tests for train-preflight.sh's DigitalOcean credential resolution.

GOL-3061. `make train-up` depends on `make train-preflight`, so anything that
makes the preflight exit non-zero takes the whole Release Train window with it.
On 2026-10-05 the preflight did exactly that: it resolved the FIRST non-empty
token it found (a stale `~/.config/doctl/config.yaml`), got HTTP 401 from the DO
volumes API, and `exit 2`-ed -- while the `op://` `do_token` that `make qa-l3-up`
itself injects was healthy the whole time. A dead credential must fall through to
the next source, not gate the train.

These tests drive the real script with `curl` and `op` shimmed onto PATH, so they
assert behaviour (which credential actually gets used) rather than grepping for a
comment. No third-party imports -- this runs on a bare `python3` in CI.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "train-preflight.sh")

GOOD = "dop_v1_good_token"

# The four volumes the preflight asserts on, shaped like the DO API's response.
VOLUMES_JSON = (
    '{"volumes":['
    '{"name":"nyc3-grove-prod-odoo-filestore","droplet_ids":[1]},'
    '{"name":"nyc3-grove-prod-blogs-data","droplet_ids":[2]},'
    '{"name":"nyc3-grove-qa-l3-odoo-filestore","droplet_ids":[]},'
    '{"name":"nyc3-grove-qa-l3-caddy-data","droplet_ids":[]}'
    "]}"
)

# Honours the exact invocation the script makes: -o <file> -w '%{http_code}'.
# Returns 200 + the volume list for GOOD only; every other bearer gets a 401.
CURL_SHIM = textwrap.dedent(
    f"""\
    #!/usr/bin/env python3
    import sys
    args = sys.argv[1:]
    out, token = None, ""
    for i, a in enumerate(args):
        if a == "-o":
            out = args[i + 1]
        if a == "-H" and args[i + 1].startswith("Authorization: Bearer "):
            token = args[i + 1].split("Bearer ", 1)[1]
    body, code = ({VOLUMES_JSON!r}, "200") if token == "{GOOD}" else ('{{"id":"Unauthorized"}}', "401")
    if out:
        open(out, "w").write(body)
    sys.stdout.write(code)
    """
)

OP_SHIM_TEMPLATE = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # `op read <ref>` -- emits %s, or fails like a locked/absent item.
    if [[ "${1:-}" == "read" ]]; then
      %s
    fi
    exit 1
    """
)


def write_exe(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


class PreflightCredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="preflight-creds-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.bin)
        os.makedirs(self.home)
        write_exe(os.path.join(self.bin, "curl"), CURL_SHIM)

    def with_op(self, token):
        """Put an `op` on PATH that resolves the DO token ref to `token`."""
        body = f'printf %s "{token}"; exit 0' if token else "exit 1"
        write_exe(os.path.join(self.bin, "op"), OP_SHIM_TEMPLATE % (token or "nothing", body))

    def doctl_config(self, token):
        cfg = os.path.join(self.tmp, "doctl.yaml")
        with open(cfg, "w") as fh:
            fh.write(f"access-token: {token}\n")
        return cfg

    def run_preflight(self, env=None, doctl_paths=""):
        # /usr/bin last so the shimmed curl/op win; coreutils still resolve.
        base = {
            "PATH": self.bin + ":/usr/local/bin:/usr/bin:/bin",
            "HOME": self.home,
            "DOCTL_CONFIG_PATHS": doctl_paths,
            # [2] (state locking) is a known-unverified exemption and is not what
            # these tests are about -- keep the process exit code about [1].
            "TRAIN_PREFLIGHT_WARN_ONLY": "1",
        }
        base.update(env or {})
        return subprocess.run(
            ["bash", SCRIPT],
            cwd=REPO,
            env=base,
            capture_output=True,
            text=True,
            timeout=60,
        )

    # --- the regression this file exists for -------------------------------
    def test_stale_doctl_token_falls_through_to_the_vault(self):
        """A present-but-401 doctl config must NOT end the run (GOL-3061)."""
        self.with_op(GOOD)
        res = self.run_preflight(doctl_paths=self.doctl_config("dop_v1_stale_token"))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("rejected (HTTP 401)", res.stdout)
        self.assertIn("authenticated via op://", res.stdout)
        self.assertIn("nyc3-grove-qa-l3-odoo-filestore exists", res.stdout)

    def test_vault_alone_is_enough(self):
        """A host with `op` and no local DO credential at all still passes [1]."""
        self.with_op(GOOD)
        res = self.run_preflight()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("authenticated via op://", res.stdout)

    def test_explicit_env_token_wins_over_the_vault(self):
        """DIGITALOCEAN_TOKEN is the documented override and is tried first."""
        self.with_op("dop_v1_vault_would_also_work")
        res = self.run_preflight(env={"DIGITALOCEAN_TOKEN": GOOD})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("authenticated via DIGITALOCEAN_TOKEN", res.stdout)
        self.assertNotIn("rejected", res.stdout)

    def test_tf_var_do_token_is_accepted(self):
        """Running inside `op run --env-file=.env.op` needs no extra plumbing."""
        self.with_op(None)
        res = self.run_preflight(env={"TF_VAR_do_token": GOOD})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("authenticated via TF_VAR_do_token", res.stdout)

    # --- the failure modes still have to fail ------------------------------
    def test_no_credential_at_all_exits_2(self):
        self.with_op(None)
        res = self.run_preflight()
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("no DigitalOcean token", res.stderr)

    def test_every_credential_rejected_exits_2(self):
        """Exhausting the candidates is still a hard stop, not a silent pass."""
        self.with_op("dop_v1_also_stale")
        res = self.run_preflight(
            env={"DIGITALOCEAN_TOKEN": "dop_v1_stale_env"},
            doctl_paths=self.doctl_config("dop_v1_stale_doctl"),
        )
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("every DigitalOcean credential was rejected", res.stderr)
        self.assertIn("DIGITALOCEAN_TOKEN", res.stderr)

    def test_duplicate_config_paths_are_probed_once(self):
        """HOME=/paperclip makes two default search entries the same file."""
        self.with_op(GOOD)
        cfg = self.doctl_config("dop_v1_stale_token")
        res = self.run_preflight(doctl_paths=f"{cfg}:{cfg}")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(res.stdout.count("rejected (HTTP 401)"), 1, res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2, argv=[sys.argv[0]])
