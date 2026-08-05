/**
 * Thin Discord REST helpers (bot token + interaction webhooks). Uses global
 * `fetch` — no discord.js gateway dependency (Phase 1 is HTTP-interactions +
 * scheduled push only; no always-on socket needed).
 */
import { DISCORD_API_BASE } from "./config.ts";
import type { DiscordMessage } from "./render.ts";

/**
 * Percent-encode a value before it is interpolated into a Discord API URL PATH.
 *
 * Every path segment below comes from data we do not author: the interaction
 * token arrives in the webhook body, the channel id from config. Even though
 * `server.ts` Ed25519-verifies each interaction before we ever read its token,
 * an unencoded segment is a request-forgery primitive — a value containing
 * `../` or a query/fragment character re-points the request at a DIFFERENT
 * Discord API endpoint than intended (CodeQL js/request-forgery, alert 530).
 * Encoding removes the primitive at the sink, so the guarantee no longer rests
 * on upstream validation alone.
 */
function urlSegment(value: string): string {
  return encodeURIComponent(value);
}

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
  const res = await fetchImpl(`${DISCORD_API_BASE}/channels/${urlSegment(channelId)}/messages`, {
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
    `${DISCORD_API_BASE}/webhooks/${urlSegment(appId)}/${urlSegment(interactionToken)}/messages/@original`,
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
