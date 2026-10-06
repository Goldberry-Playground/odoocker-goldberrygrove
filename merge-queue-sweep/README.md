# merge-queue-sweep — the unattended arming sweep (GOL-3159)

Runs `scripts/ci/merge-queue-arm-sweep.sh --apply` every 5 minutes against all
three Goldberry repos, so that agent PRs are armed for auto-merge under the
**App** identity before `auto-approve.yml` can enqueue them under
`GITHUB_TOKEN` and build a dead merge group.

## Why this exists

`scripts/ci/merge-queue-arm-automerge.sh` landed in all three repos (GOL-3118 in
grove-sites, GOL-3150 in the other two) and it works. But nothing ran it on a
schedule anywhere, so the fix depended on every agent remembering to arm its own
PR in the same breath as opening it. That moved the chore rather than deleting
it: an agent that forgets still wedges the queue, and the queue is sequential —
one dead entry holds up every healthy entry behind it for the full ~30-minute
eviction timeout.

Measured live on 2026-10-06, two PRs that had been sitting ejected for hours:

| PR | enqueued by `github-actions` | armed by the sweep | merged |
| --- | --- | --- | --- |
| grove-odoo-modules #323 | 08:40:53Z → 0 runs, ejected | 09:47:34Z | **09:52:17Z** |
| grove-sites #1000 | 06:51:13Z → 0 runs, ejected | 09:47:37Z | **09:51:51Z** |

## Why it cannot run in GitHub Actions

The default `GITHUB_TOKEN` is the identity that causes the wedge. A sweep running
on it would arm auto-merge as `github-actions` and rebuild the same dead merge
group it exists to prevent. It also cannot run anywhere else in this repo's
fleet: the sweep needs an **App installation token**, and `gh-token-broker` — the
sole holder of the App private key (ADR-0001: *agents never see the root key*) —
resolves only by service name on the agent-plane docker network. That is the one
constraint that fixes where this runs.

## Deploying it

`compose.agent-plane.yml` is the service definition. It is **not wired into any
stack in this repo** — every other compose file here targets the app or
observability droplets, where the broker does not resolve. On the agent-plane
host:

```bash
# 1. which network is the broker on?
docker inspect gh-token-broker \
  --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}'

# 2. AGENT_PLANE_NETWORK=<that value>, then bring it up as an overlay
docker compose -f <agenticos-compose.yml> -f merge-queue-sweep/compose.agent-plane.yml \
  up -d --build merge-queue-sweep

# 3. verify without waiting for a tick — dry run, arms nothing
docker compose exec merge-queue-sweep /app/scripts/ci/merge-queue-arm-sweep.sh
```

Step 3 should print a `repo=…` banner per repo and then either `nothing to arm`
or `DRY-RUN would arm #N`. A `FATAL: broker key not readable` means the key
bind-mount is wrong; a `FATAL: GraphQL returned no repository data` means the
broker minted a token without access to that repo.

**Merged to `main` is not applied.** Until that compose edit happens on the host,
this directory changes nothing.

## Cadence — why 5 minutes

The sweep only helps if it fires between a PR opening and `auto-approve.yml`
approving it, because approval and the wedging enqueue are **6 seconds** apart in
practice. So the budget is the whole open→enqueue window. Over the last 39 agent
PRs enqueued by `github-actions[bot]` across the three repos:

```
min 1.6 min | p10 4.2 min | median 23 min | p90 183 min | max 582 min
```

Expected catch rate at interval `T` (`P = min(1, window/T)`):

| T | 1 min | 5 min | 10 min | 15 min | 30 min | 60 min | 4 h |
| --- | --- | --- | --- | --- | --- | --- | --- |
| catch | 100% | **96%** | 88% | 83% | 69% | 49% | 20% |

5 minutes is the knee: finer buys the last 4% for 5× the API calls, coarser
loses real wedges. A fire costs ~2 API calls per repo against a 5000/hour
installation-token budget.

**Polling cannot reach 100%.** odoocker #813 was enqueued 1.6 minutes after it
opened; no practical interval beats that reliably. The thing that would make
cadence a *latency* knob instead of a *correctness* knob is removing the
`GITHUB_TOKEN` enqueue from `auto-approve.yml`, so arming becomes the only
enqueue path and an unarmed PR merely waits for the next sweep instead of
wedging. That is tracked separately and must not land before this sweep is
actually running.

## Alerting — two failures, two channels

A sweep that silently stops is a different failure from a sweep that runs and
errors, and **neither channel can detect the other's failure**:

| env var | detects | mechanism |
| --- | --- | --- |
| `SWEEP_HEARTBEAT_URL` | the sweep **stopped running** | pinged on every completed run; the Healthchecks.io grace period is what makes a stopped sweep visible within one cadence. A Discord alert can never catch this — a dead sweep sends nothing. |
| `DISCORD_OPS_WEBHOOK_URL` | the sweep **runs but cannot work** | posted after `SWEEP_FAIL_THRESHOLD` (default 3) consecutive **hard** failures on a repo, and once again on recovery. |

`merge-queue-sweep` distinguishes **hard** from **soft** failures, which is what
keeps the Discord channel worth reading:

- **hard** — the repo could not be *evaluated*: broker unreachable, token mint
  failed, GraphQL returned no repository. Not self-healing; every later run fails
  identically until someone looks. Pages.
- **soft** — the repo was evaluated and GitHub refused one individual arm (stale
  head, queue race). The next run re-reads the PR and retries, because the inner
  script is idempotent. Logged, never paged.

Both are left unset by default, so an un-configured deploy is silent rather than
noisy. Set them in the agent-plane environment.

## What it will not do

`ARM_PROTECTED` is deliberately never set. It would pre-arm *unapproved
protected-path* PRs, which turns a human reviewer's approval into the merge
itself. That is a board decision, not a deploy knob. `ARM_UNAPPROVED=1` — which
the sweep does force, because it is the only mode that runs before approval — is
safe without one: it skips unapproved protected-path PRs using each **target**
repo's own base-branch `scripts/ci/protected-paths-carveout.mjs`, and fails
closed on every unreadable input.
