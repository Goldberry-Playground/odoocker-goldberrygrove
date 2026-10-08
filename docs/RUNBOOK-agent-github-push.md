# Runbook — agent-plane GitHub push path

**Owner:** DevOps - Terra · **Origin:** GOL-2571 · **Last verified:** 2026-09-29

If you are an agent that just failed to push or open a PR, run this first:

```bash
scripts/agent-git/github-push-doctor.sh
```

Exit 0 means the push path is healthy and the fault is in **how you invoked it** —
the script prints the copy-paste invocation that works. Do not file an outage
issue on an exit-0 result. Exit 1 means a real fault, and the script prints the
escalation block to hand to Josh.

## The designed path

`git` authenticates through a **GitHub App credential helper**, configured
globally in `/paperclip/agent-git/gitconfig`:

```
[credential "https://github.com"]
    helper = "!node /paperclip/agent-git/github-app-token.mjs"
    useHttpPath = true
```

So the ordinary command is all you need — no token handling, no URL rewriting:

```bash
git push origin HEAD:<branch-name>
```

The helper mints a short-lived installation token from the broker per operation.
Pushes by this App identity **do** trigger CI (unlike the Actions
`GITHUB_TOKEN`). For the REST API or `gh`, mint one explicitly:

```bash
export GH_TOKEN="$(node /paperclip/agent-git/github-app-token.mjs token Goldberry-Playground/odoocker-goldberrygrove)"
gh pr create --base main --head <branch-name> --title '...' --body '...'
```

Tokens expire in about an hour. Mint immediately before use; never persist one.

## Two false alarms that have each cost a heartbeat

### 1. Broker returns `owner_not_allowed` — wrong param shape, not an allowlist gap

The broker takes **`owner` and `repo` as two separate query params**:

```bash
# WORKS
curl -s -H "Authorization: Bearer $(cat "$GH_BROKER_API_KEY_FILE")" \
  "$GH_TOKEN_BROKER_URL/token?owner=Goldberry-Playground&repo=odoocker-goldberrygrove"
# -> {"token":"ghs_...","expires_at":"..."}

# 403 owner_not_allowed — full slug in repo= leaves owner= EMPTY
curl ... "$GH_TOKEN_BROKER_URL/token?repo=Goldberry-Playground/odoocker-goldberrygrove"
```

The trap is that this failure *looks* server-side: you get a structured `403
{"error":"owner_not_allowed"}` rather than a `400`, and a no-param `/token`
returns the same thing. GOL-2571 read that as "the allowlist is unset and
defaulting closed, so **all** repos are affected". It is not — an absent `owner`
param is simply an empty owner, and an empty owner is not on the allowlist. Both
requests fail for the same client-side reason.

**`owner_not_allowed` is only a real allowlist gap if you passed a correct
non-empty `owner=`.** Step 3 of the doctor script is that exact test.

### 2. `gh auth status` says the token is invalid — expected, and nothing reads it

```
X Failed to log in to github.com account agenticos-developer[bot] (/paperclip/.config/gh/hosts.yml)
  - The token in /paperclip/.config/gh/hosts.yml is invalid.
```

`hosts.yml` holds a `ghs_` **App installation token**, which GitHub expires after
**one hour**. It was written 2026-08-02 and has been dead since 2026-08-02 21:07.
It is a dead artifact, not a credential that recently lapsed — so "this expired a
while ago and nobody noticed" is not a finding, and re-running `gh auth login`
would only re-create the same one-hour artifact.

Nothing depends on it: `git` uses the credential helper, and `gh` reads `GH_TOKEN`
from the environment, which **overrides** `hosts.yml`. Verified 2026-09-29:
`GH_TOKEN=$(...) gh auth status` reports `✓ Logged in ... (GH_TOKEN)` on the same
run where the `hosts.yml` line still says invalid. Ignore that line.

Also expected, and not a permissions problem: `gh api repos/<owner>/<repo>` shows
`permissions.push=false` for an installation token. The authoritative test is
whether `git push` / `git ls-remote` succeeds — it does.

## Probing the broker

The broker is a **separate container**, not a port on the agent host:

```bash
curl -s "$GH_TOKEN_BROKER_URL/health"     # http://gh-token-broker:9099 -> 200 {"status":"ok"}
node /paperclip/agent-git/github-app-token.mjs health
```

`curl localhost:9099` is **always** connection-refused and is not evidence of an
outage. That single mistake produced false "broker down" escalations on GOL-2404,
GOL-2273, GOL-1545 and GOL-2254.

## Known real limits of the App token (do not mistake for outages)

These are categorical scope limits, documented so nobody re-debugs them:

| Operation | Result | Why |
|---|---|---|
| `gh workflow run` (workflow_dispatch) | 403 | App token is scoped out of `actions:write` |
| Approving an `action_required` run | 403 | GitHub bars App tokens from run approval — needs a human maintainer |
| `gh secret list` | empty | no secrets scope |
| `GET /user` | 403 | installation token, not a user token |

## If the broker is genuinely down

Review-only work is **not** blocked — PR refs fetch unauthenticated:

```bash
git -c credential.helper= fetch origin refs/pull/<N>/head
git diff <base-sha>..<head-sha> -- <files>
```

Commit locally, record the branch name and SHA on the issue so the work is
visible, mark the issue `blocked` naming Josh as the unblock owner, and paste the
doctor script's escalation block.
