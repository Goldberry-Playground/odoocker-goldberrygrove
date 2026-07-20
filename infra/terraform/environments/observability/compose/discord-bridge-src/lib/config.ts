/**
 * Environment/config resolution for the Discord bridge.
 *
 * Secrets are provisioned by Josh into 1Password `Grove Infra` (tracked in
 * GOL-263) and injected as env vars at run time. Nothing is committed. The
 * 1P item key → env var mapping is documented in `.env.example`.
 */

/** Buffer organization id (Goldberry Grove). Stable, not a secret. */
export const BUFFER_ORG_ID_DEFAULT = "5a3295c5a73330590e63e1bb";

/** Buffer GraphQL endpoint (verified live 2026-07-11 — not graph.buffer.com). */
export const BUFFER_GRAPHQL_URL = "https://api.buffer.com/";

/** Discord API base. */
export const DISCORD_API_BASE = "https://discord.com/api/v10";

export interface BridgeConfig {
  bufferToken: string;
  bufferOrgId: string;
  discordBotToken: string;
  discordAppId: string;
  discordPublicKey: string;
  /** Channel the weekly digest is auto-posted to (#weekly-insights). */
  weeklyInsightsChannelId: string;
}

class MissingEnvError extends Error {
  constructor(names: string[]) {
    super(
      `discord-bridge: missing required env var(s): ${names.join(", ")}. ` +
        `See apps/discord-bridge/.env.example (provisioned via GOL-263 → 1P Grove Infra).`,
    );
    this.name = "MissingEnvError";
  }
}

function req(env: NodeJS.ProcessEnv, name: string, missing: string[]): string {
  const v = env[name];
  if (!v || v.trim() === "") {
    missing.push(name);
    return "";
  }
  return v.trim();
}

/**
 * Resolve full config for jobs that talk to both Buffer and Discord
 * (the scheduled digest, the interactions server). Throws listing every
 * missing var so operators fix them in one pass.
 */
export function loadConfig(env: NodeJS.ProcessEnv = process.env): BridgeConfig {
  const missing: string[] = [];
  const cfg: BridgeConfig = {
    bufferToken: req(env, "BUFFER_API_TOKEN", missing),
    bufferOrgId: (env.BUFFER_ORG_ID?.trim() || BUFFER_ORG_ID_DEFAULT),
    discordBotToken: req(env, "DISCORD_BOT_TOKEN", missing),
    discordAppId: req(env, "DISCORD_APP_ID", missing),
    discordPublicKey: req(env, "DISCORD_PUBLIC_KEY", missing),
    weeklyInsightsChannelId: req(env, "DISCORD_WEEKLY_INSIGHTS_CHANNEL_ID", missing),
  };
  if (missing.length) throw new MissingEnvError(missing);
  return cfg;
}

/**
 * Resolve just the Buffer credentials — used by the `/insights` read path and
 * tests that never touch Discord. Keeps the read-only insights token isolated
 * from the (future, Phase 2) write-scoped token.
 */
export function loadBufferConfig(env: NodeJS.ProcessEnv = process.env): {
  bufferToken: string;
  bufferOrgId: string;
} {
  const missing: string[] = [];
  const bufferToken = req(env, "BUFFER_API_TOKEN", missing);
  if (missing.length) throw new MissingEnvError(missing);
  return { bufferToken, bufferOrgId: env.BUFFER_ORG_ID?.trim() || BUFFER_ORG_ID_DEFAULT };
}
