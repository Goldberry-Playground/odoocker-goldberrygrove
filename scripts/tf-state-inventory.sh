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
# USAGE
#   op run --env-file=infra/terraform/environments/production/.env.op -- \
#     bash scripts/tf-state-inventory.sh
#
#   # ...or against a candidate backend you are evaluating (R2, MinIO, S3):
#   GROVE_S3_HOST=<account>.r2.cloudflarestorage.com \
#   GROVE_TF_STATE_BUCKET=grove-tf-state \
#     bash scripts/tf-state-inventory.sh
#
# ENV
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY  required (Spaces/R2/S3 key pair)
#   GROVE_TF_STATE_BUCKET                      default: grove-tf-state
#   GROVE_S3_HOST                              default: nyc3.digitaloceanspaces.com
#   GROVE_S3_REGION                            default: us-east-1 (SigV4 scope;
#                                              Spaces and R2 both want this)
#
# EXIT CODES
#   0  inventory printed, no stale locks
#   1  inventory printed, at least one stale .tflock is present
#   2  bad credentials / LIST failed
set -euo pipefail

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

states = sorted(k for k in keys if k.endswith(".tfstate"))
locks = sorted(k for k in keys if k.endswith(".tflock"))

print(f"bucket           : {BUCKET} @ {HOST}")
print(f"objects          : {len(keys)}  ({len(states)} .tfstate, {len(locks)} .tflock)")

rows, total, nonempty = [], 0, 0
for key in states:
    status, body = s3("GET", f"/{BUCKET}/{urllib.parse.quote(key)}")
    if status != 200:
        rows.append((key, f"HTTP{status}", "-", "-"))
        continue
    try:
        doc = json.loads(body)
    except ValueError:
        rows.append((key, "UNPARSED", "-", "-"))
        continue
    count = sum(
        len(r.get("instances", []))
        for r in doc.get("resources", [])
        if r.get("mode") == "managed"
    )
    total += count
    if count:
        nonempty += 1
    rows.append((key, str(count), str(doc.get("serial")), str(doc.get("terraform_version"))))

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
