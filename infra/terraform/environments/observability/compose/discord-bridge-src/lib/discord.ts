/**
 * Thin Discord REST helpers (bot token + interaction webhooks). Uses global
 * `fetch` — no discord.js gateway dependency (Phase 1 is HTTP-interactions +
 * scheduled push only; no always-on socket needed).
 */
import { DISCORD_API_BASE } from "./config.ts";
import type { DiscordMessage } from "./render.ts";

async function assertOk(res: Response, action: string): Promise<void> {
  if (!res.ok) {
    let body = "";
    try {
      body = (await res.text()).slice(0, 400);
    } catch {
      /* ignore */
    }
    throw new Error(`Discord ${action} failed: HTTP ${res.status} ${body}`);
  }
}

/** Post the digest (or any message) to a channel using the bot token. */
export async function postChannelMessage(
  botToken: string,
  channelId: string,
  message: DiscordMessage,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const res = await fetchImpl(`${DISCORD_API_BASE}/channels/${channelId}/messages`, {
    method: "POST",
    headers: { Authorization: `Bot ${botToken}`, "Content-Type": "application/json" },
    body: JSON.stringify(message),
  });
  await assertOk(res, "postChannelMessage");
}

/**
 * Edit the original response to a deferred interaction. The interaction token
 * authenticates the call (no bot token). Valid for 15 minutes after the
 * interaction.
 */
export async function editInteractionOriginal(
  appId: string,
  interactionToken: string,
  message: DiscordMessage,
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const res = await fetchImpl(
    `${DISCORD_API_BASE}/webhooks/${appId}/${interactionToken}/messages/@original`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message),
    },
  );
  await assertOk(res, "editInteractionOriginal");
}

/** The `/insights [period]` slash-command definition. */
export const INSIGHTS_COMMAND = {
  name: "insights",
  description: "Pull the Buffer social-analytics digest (Threads-led, read-only).",
  type: 1,
  options: [
    {
      name: "period",
      description: "Reporting window (default: last 7 days)",
      type: 3, // STRING
      required: false,
      choices: [
        { name: "Last 7 days", value: "last7d" },
        { name: "Last 30 days", value: "last30d" },
        { name: "Last 90 days", value: "last90d" },
      ],
    },
  ],
};

/** Register global application (slash) commands. Overwrites the full set. */
export async function registerGlobalCommands(
  botToken: string,
  appId: string,
  commands: unknown[],
  fetchImpl: typeof fetch = fetch,
): Promise<void> {
  const res = await fetchImpl(`${DISCORD_API_BASE}/applications/${appId}/commands`, {
    method: "PUT",
    headers: { Authorization: `Bot ${botToken}`, "Content-Type": "application/json" },
    body: JSON.stringify(commands),
  });
  await assertOk(res, "registerGlobalCommands");
}
