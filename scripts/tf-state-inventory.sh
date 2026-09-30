#!/usr/bin/env bash
# Read-only inventory of the Terraform remote-state bucket (GOL-2755).
#
# WHY THIS EXISTS
# ---------------
# GOL-2584 found that `use_lockfile = true` is a NO-OP on DO Spaces: Spaces
# accepts a conditional `PUT <key>.tflock` with `If-None-Match: *` over an
# existing object (200, not 412), so Terraform's S3-native locking provides
# ZERO mutual exclusion in every Grove env. `scripts/tf-state-lock-check.sh`
# (GOL-2584) is the enforcement probe and the advisory guard. THIS script is
# the complementary inventory: it answers the questions you need answered
# *around* a state-backend decision or migration, without ever writing.
#
#   1. How big is the migration really?  One line per state object with its
#      managed-resource count, serial, and the Terraform version that last
#      wrote it. Empty states (torn-down previews, retired envs) do not need
#      migrating and should not be counted in the estimate.
#   2. Are there stale locks right now?  A leftover `<key>.tflock` is the
#      residue of a run that died between acquire and release. Because Spaces
#      does not enforce the conditional PUT, a stale lock is ALSO the thing
#      that makes the next run die with
#      `Error releasing the state lock ... StatusCode: 404`.
#   3. Did a migration preserve everything?  Run it before and after and diff:
#      same keys, same managed counts. A resource that silently did not make
#      the trip shows up as a count delta, not as a 3am outage.
#
# It is deliberately read-only: GET and LIST only, no PUT/DELETE anywhere. It
# is safe to run against production state at any time, including mid-window.
#
# WHY THE PARITY CHECK IS A MODE AND NOT AN EYEBALL (GOL-2755)
# -----------------------------------------------------------
# ADR-011's migration shape says "same key, same managed count" before and
# after. Count parity is necessary but NOT sufficient, and the two ways it
# lies are both silent:
#
#   * Same count, different resources. Losing one resource while another gets
#     imported nets to zero. The state reads fine; the next apply proposes a
#     create for something that already exists (or a destroy for something
#     load-bearing).
#   * Same count, NEW lineage. `terraform init` without `-migrate-state`
#     against an empty backend happily starts a fresh state file. The counts
#     can coincide; the old state is now an orphan and Terraform has
#     forgotten it ever managed the real resources. `lineage` is the only
#     field that catches this, and nobody reads it by hand.
#
# So `--json` emits a machine fingerprint (lineage + serial + the SORTED set
# of managed resource addresses + a digest of that set) and `--compare` diffs
# two of them and sets an exit code. Take the baseline before touching
# anything; the artifact is the rollback decision record.
#
# USAGE
#   # human table (unchanged)
#   op run --env-file=infra/terraform/environments/production/.env.op -- \
#     bash scripts/tf-state-inventory.sh
#
#   # pre-migration baseline, and the parity gate after
#   ... -- bash scripts/tf-state-inventory.sh --json > /tmp/state-before.json
#   ... -- bash scripts/tf-state-inventory.sh --compare /tmp/state-before.json
#
#   # ...or against a candidate backend you are evaluating (R2, MinIO, S3):
#   GROVE_S3_HOST=<account>.r2.cloudflarestorage.com \
#   GROVE_S3_REGION=auto \
#   GROVE_TF_STATE_BUCKET=grove-tf-state \
#     bash scripts/tf-state-inventory.sh --compare /tmp/state-before.json
#
#   The endpoint/bucket are RECORDED in the fingerprint but deliberately NOT
#   compared -- changing them is the entire point of the migration.
#
# ENV
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  required (Spaces/R2/S3 key pair)
#   GROVE_TF_STATE_BUCKET                      default: grove-tf-state
#   GROVE_S3_HOST                              default: nyc3.digitaloceanspaces.com
#   GROVE_S3_REGION                            default: us-east-1 (SigV4 scope).
#                                              Spaces wants us-east-1; **R2
#                                              requires `auto`** or SigV4 fails
#                                              (GOL-2760 / PR #793).
#
# EXIT CODES
#   0  inventory printed / fingerprint emitted / parity holds
#   1  inventory printed, at least one stale .tflock is present (table mode
#      only -- a stale lock is an operational warning, not a parity failure,
#      so --compare reports it and keeps its own exit code)
#   2  bad credentials / LIST failed / unreadable baseline
#   4  --compare only: PARITY VIOLATION. A state key vanished, a lineage
#      changed, a serial went backwards, or the managed-address set moved.
#      Do NOT proceed with the migration; roll back to the old backend.
set -euo pipefail

MODE=table
BASELINE=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --json)
      MODE=json
      shift
      ;;
    --compare)
      MODE=compare
      BASELINE="${2:-}"
      if [ -z "$BASELINE" ]; then
        echo "tf-state-inventory: --compare needs a baseline .json path" >&2
        exit 2
      fi
      if [ ! -r "$BASELINE" ]; then
        echo "tf-state-inventory: cannot read baseline '$BASELINE'" >&2
        exit 2
      fi
      shift 2
      ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;$d'
      exit 0
      ;;
    *)
      echo "tf-state-inventory: unknown argument '$1'" >&2
      exit 2
      ;;
  esac
done
export GROVE_INV_MODE="$MODE" GROVE_INV_BASELINE="$BASELINE"

if [ -z "${AWS_ACCESS_KEY_ID:-}" ] || [ -z "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo "tf-state-inventory: AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY unset." >&2
  echo "  Run under: op run --env-file=infra/terraform/environments/<env>/.env.op -- $0" >&2
  exit 2
fi

# Minimal SigV4 S3 client, stdlib only -- there is no awscli/boto on the ops
# hosts or the agent plane, and this must stay runnable from both.
python3 - <<'PY'
import datetime, hashlib, hmac, json, os, re, sys, urllib.error, urllib.parse, urllib.request

AK = os.environ["AWS_ACCESS_KEY_ID"]
SK = os.environ["AWS_SECRET_ACCESS_KEY"]
HOST = os.environ.get("GROVE_S3_HOST", "nyc3.digitaloceanspaces.com")
REGION = os.environ.get("GROVE_S3_REGION", "us-east-1")
BUCKET = os.environ.get("GROVE_TF_STATE_BUCKET", "grove-tf-state")
SERVICE = "s3"


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def s3(method, path, query=""):
    """Signed GET/LIST. No write verbs are reachable from this function."""
    if method not in ("GET", "HEAD"):
        raise AssertionError("tf-state-inventory is read-only")
    body = b""
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    headers = {"host": HOST, "x-amz-content-sha256": payload_hash, "x-amz-date": amzdate}
    signed = ";".join(sorted(headers))
    canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canon = f"{method}\n{path}\n{query}\n{canon_headers}\n{signed}\n{payload_hash}"
    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"
    sts = (
        "AWS4-HMAC-SHA256\n"
        f"{amzdate}\n{scope}\n" + hashlib.sha256(canon.encode()).hexdigest()
    )
    k = _sign(("AWS4" + SK).encode(), datestamp)
    k = _sign(k, REGION)
    k = _sign(k, SERVICE)
    k = _sign(k, "aws4_request")
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    url = f"https://{HOST}{path}" + (f"?{query}" if query else "")
    req = urllib.request.Request(url, method=method)
    for key, value in headers.items():
        req.add_header(key, value)
    req.add_header(
        "Authorization",
        f"AWS4-HMAC-SHA256 Credential={AK}/{scope}, SignedHeaders={signed}, Signature={sig}",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


keys, token = [], None
while True:
    params = {"list-type": "2", "max-keys": "1000"}
    if token:
        params["continuation-token"] = token
    query = "&".join(
        f"{k}={urllib.parse.quote(v, safe='')}" for k, v in sorted(params.items())
    )
    status, body = s3("GET", f"/{BUCKET}", query)
    if status != 200:
        print(f"tf-state-inventory: LIST {BUCKET} failed: HTTP {status}", file=sys.stderr)
        print(body[:400].decode(errors="replace"), file=sys.stderr)
        sys.exit(2)
    text = body.decode(errors="replace")
    keys += re.findall(r"<Key>([^<]+)</Key>", text)
    nxt = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", text)
    if re.search(r"<IsTruncated>true</IsTruncated>", text) and nxt:
        token = nxt.group(1)
    else:
        break

MODE = os.environ.get("GROVE_INV_MODE", "table")
BASELINE = os.environ.get("GROVE_INV_BASELINE", "")

states = sorted(k for k in keys if k.endswith(".tfstate"))
locks = sorted(k for k in keys if k.endswith(".tflock"))


def address_of(resource, instance):
    """Reconstruct the Terraform address Terraform itself would print."""
    parts = []
    if resource.get("module"):
        parts.append(resource["module"])
    parts.append(f"{resource['type']}.{resource['name']}")
    addr = ".".join(parts)
    index = instance.get("index_key")
    if isinstance(index, bool) or index is None:
        return addr
    if isinstance(index, int):
        return f"{addr}[{index}]"
    return f'{addr}["{index}"]'


# ---- one signed GET per state object; this is the whole data collection ----
snapshot = {
    "schema": "grove-tf-state-fingerprint/1",
    "generated_at": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    # Recorded for the audit trail, NEVER compared: the endpoint moving is the
    # migration.
    "endpoint": {"bucket": BUCKET, "host": HOST, "region": REGION},
    "object_count": len(keys),
    "locks": locks,
    "states": {},
}
errors = []
for key in states:
    status, body = s3("GET", f"/{BUCKET}/{urllib.parse.quote(key)}")
    if status != 200:
        errors.append((key, f"HTTP{status}"))
        snapshot["states"][key] = {"error": f"HTTP{status}"}
        continue
    try:
        doc = json.loads(body)
    except ValueError:
        errors.append((key, "UNPARSED"))
        snapshot["states"][key] = {"error": "UNPARSED"}
        continue
    addresses = sorted(
        address_of(r, inst)
        for r in doc.get("resources", [])
        if r.get("mode") == "managed"
        for inst in r.get("instances", [])
    )
    snapshot["states"][key] = {
        "lineage": doc.get("lineage"),
        "serial": doc.get("serial"),
        "terraform_version": doc.get("terraform_version"),
        "managed": len(addresses),
        "addresses": addresses,
        "addresses_sha256": hashlib.sha256(
            "\n".join(addresses).encode()
        ).hexdigest(),
    }

if MODE == "json":
    json.dump(snapshot, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    sys.exit(0)

if MODE == "compare":
    try:
        with open(BASELINE) as fh:
            before = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"tf-state-inventory: unreadable baseline: {exc}", file=sys.stderr)
        sys.exit(2)
    if before.get("schema") != snapshot["schema"]:
        print(
            "tf-state-inventory: baseline is not a "
            f"{snapshot['schema']} document (got {before.get('schema')!r}).",
            file=sys.stderr,
        )
        sys.exit(2)

    b_states = before.get("states", {})
    b_ep = before.get("endpoint", {})
    print("state parity check")
    print(
        f"  before : {b_ep.get('bucket')} @ {b_ep.get('host')}"
        f"  ({before.get('generated_at')})"
    )
    print(
        f"  after  : {BUCKET} @ {HOST}"
        f"  ({snapshot['generated_at']})"
    )
    print()

    violations, warnings = [], []
    if errors:
        violations += [f"{key}: could not be read now ({why})" for key, why in errors]

    for key in sorted(set(b_states) | set(snapshot["states"])):
        b = b_states.get(key)
        a = snapshot["states"].get(key)
        if b is None:
            warnings.append(f"{key}: NEW since the baseline (not a loss, but explain it)")
            print(f"  +  {key}  new")
            continue
        if a is None:
            violations.append(f"{key}: PRESENT in the baseline, MISSING now")
            print(f"  !! {key}  MISSING")
            continue
        if b.get("error") or a.get("error"):
            violations.append(
                f"{key}: unreadable on one side "
                f"(before={b.get('error')}, after={a.get('error')})"
            )
            print(f"  !! {key}  unreadable")
            continue
        notes = []
        if a["lineage"] != b["lineage"]:
            violations.append(
                f"{key}: LINEAGE CHANGED {b['lineage']} -> {a['lineage']} "
                "— this state was re-initialised, not migrated; the old one is "
                "now an orphan"
            )
            notes.append("lineage")
        if (a["serial"] or 0) < (b["serial"] or 0):
            violations.append(
                f"{key}: serial WENT BACKWARDS {b['serial']} -> {a['serial']} "
                "— an older copy of the state was promoted"
            )
            notes.append("serial")
        if a["addresses_sha256"] != b["addresses_sha256"]:
            gone = sorted(set(b["addresses"]) - set(a["addresses"]))
            new = sorted(set(a["addresses"]) - set(b["addresses"]))
            violations.append(
                f"{key}: managed address set moved "
                f"(-{len(gone)}/+{len(new)}, count {b['managed']} -> {a['managed']})"
            )
            for addr in gone:
                print(f"       - {addr}")
            for addr in new:
                print(f"       + {addr}")
            notes.append("addresses")
        if a["terraform_version"] != b["terraform_version"]:
            warnings.append(
                f"{key}: written by {b['terraform_version']} -> "
                f"{a['terraform_version']} (expected if the migrating host is "
                "newer; Terraform will not write a state a NEWER version touched)"
            )
        flag = "!!" if notes else "ok"
        print(
            f"  {flag} {key}  managed={a['managed']} serial={a['serial']} "
            f"lineage={(a['lineage'] or '?')[:8]}"
            + (f"  <- {', '.join(notes)}" if notes else "")
        )

    if locks:
        print()
        print("note: stale .tflock present (not a parity failure):")
        for lock in locks:
            print(f"  {lock}")

    print()
    for warn in warnings:
        print(f"WARNING  {warn}")
    if violations:
        for bad in violations:
            print(f"VIOLATION  {bad}")
        print()
        print(
            f"PARITY FAILED ({len(violations)} violation(s)). Do NOT continue the "
            "migration. Roll back: restore the old backend config and "
            "`terraform init -reconfigure`."
        )
        sys.exit(4)
    print(
        f"PARITY OK — {len(b_states)} state object(s), same lineages, same "
        "managed addresses."
    )
    sys.exit(0)

# ---- default: the human table ----
print(f"bucket           : {BUCKET} @ {HOST}")
print(f"objects          : {len(keys)}  ({len(states)} .tfstate, {len(locks)} .tflock)")

rows, total, nonempty = [], 0, 0
for key in states:
    st = snapshot["states"][key]
    if st.get("error"):
        rows.append((key, st["error"], "-", "-"))
        continue
    total += st["managed"]
    if st["managed"]:
        nonempty += 1
    rows.append(
        (key, str(st["managed"]), str(st["serial"]), str(st["terraform_version"]))
    )

width = max([len(r[0]) for r in rows] + [len("state key")])
print()
print(f"{'state key':<{width}} {'managed':>8} {'serial':>7}  written-by")
print(f"{'-' * width} {'-' * 8:>8} {'-' * 7:>7}  {'-' * 10}")
for key, count, serial, version in rows:
    print(f"{key:<{width}} {count:>8} {serial:>7}  {version}")

print()
print(f"managed resource instances, all states : {total}")
print(f"states that actually hold resources    : {nonempty} of {len(states)}")
print("  (empty states are torn-down previews / retired envs: nothing to migrate)")

if locks:
    print()
    print("STALE LOCK(S) PRESENT -- a run died between acquire and release:")
    for lock in locks:
        print(f"  {lock}")
    print("  The next run against that key will fail on release with a 404.")
    print("  Confirm no terraform process is live, then remove the object by hand.")
    sys.exit(1)
PY
