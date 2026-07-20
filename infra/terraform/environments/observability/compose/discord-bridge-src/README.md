# @grove/discord-bridge

Discord bridge for the Grove CMO loop. **Phase 1 (GOL-262)** ships:

1. **Weekly Buffer digest** — auto-posted to `#weekly-insights` every Monday
   08:00 ET (GitHub Actions cron → `digest-cli.ts`).
2. **`/insights [7d|30d|90d]`** slash command — the same digest on demand.
3. **HTTP Interactions endpoint** — Ed25519-verified, powers the slash command
   and lays groundwork for Phase 2 buttons/modals.

Implements the CMO payload contract frozen in
[GOL-233 §1](https://.../GOL-233) (Sora) against the architecture from
[GOL-234](https://.../GOL-234) (Alice). Board go/no-go accepted 2026-07-11.

## What it does NOT do (later phases)

- Content-suggestion cards / approve-revise-reject flow → Phase 2 (GOL-259).
- `/idea` intake → Phase 3 (GOL-260).
- Buffer **write/schedule** scope → Phase 2 gate (a *separate* write-scoped
  token; this app uses only the read-only insights token and never mutates).
- Auto-publish → off by design (`publish_mode: draft_only`).

## Design notes

- **Zero runtime dependencies.** Buffer + Discord are reached over `fetch`;
  Ed25519 verification uses Node's built-in `crypto`; the interactions server is
  `node:http`. This keeps the deploy tiny and the dependency-audit surface at
  zero.
- **Read-only.** Only the `buffer_api_token` insights token is used. The digest
  makes **zero demographic claims** (Buffer exposes none — GOL-227).
- **Threads-led.** Channels rank by engagement; Threads is ~97% of it (GOL-226).
- Source files use explicit `.ts` import extensions so the entrypoints run
  directly under Node's native type-stripping (`node --experimental-strip-types`)
  with no build step or bundler.

## Layout

```
lib/
  config.ts        env/secret resolution (+ 1P key mapping)
  period.ts        reporting-window math (current + prior window for WoW)
  metrics.ts       Buffer metric parsing (posts / impressions / engagements)
  buffer.ts        Buffer GraphQL client (read-only): channels, metrics, top post
  digest.ts        assembles the weekly_digest payload (GOL-233 §1c)
  render.ts        weekly_digest → Discord embed + period buttons
  verify.ts        Ed25519 request verification (node:crypto)
  interactions.ts  routes verified interactions (PING / /insights / buttons)
  discord.ts       Discord REST (post message, edit interaction, register cmds)
  insights.ts      orchestrator: pull Buffer → build digest
  audit.ts         immutable evt_ audit records (GOL-233 §5b)
server.ts          HTTP interactions endpoint (deploy target)
digest-cli.ts      scheduled/weekly digest entrypoint (cron target)
register-commands.ts  one-time /insights registration
```

## Local / operational commands

```bash
# All env vars from 1P (see .env.example). Example with 1Password CLI:
export BUFFER_API_TOKEN=$(op read "op://Goldberry Grove - Admin/Grove Infra/buffer_api_token")

# One-time: register the /insights slash command with Discord.
pnpm --filter @grove/discord-bridge register-commands

# Post this week's digest now (used by the Monday cron).
pnpm --filter @grove/discord-bridge digest            # last7d
pnpm --filter @grove/discord-bridge digest -- last30d

# Run the interactions server (deploy behind the registered Interactions URL).
pnpm --filter @grove/discord-bridge start
```

## Deploy checklist (blocked on GOL-263 credential provisioning)

1. Josh creates the Discord app + bot, saves `discord_bot_token`,
   `discord_app_id`, `discord_public_key`, `discord_insights_channel_id` (the
   `#weekly-insights` channel the digest posts to) and `discord_approvals_channel_id`
   (the `#cmo-approvals` channel, reserved for Phase 2) to 1P `Grove Infra` (GOL-263).
2. Deploy `server.ts` to a droplet; set the Discord **Interactions Endpoint URL**
   to `https://<host>/interactions` (Discord validates it with a PING —
   verification is already implemented).
3. Run `register-commands` once.
4. Enable the `discord-digest` GitHub Actions workflow (or a droplet cron).
5. Verify: `/insights` returns a digest; Monday cron posts to `#weekly-insights`.
