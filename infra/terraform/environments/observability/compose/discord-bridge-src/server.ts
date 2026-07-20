/**
 * HTTP Interactions endpoint for the Discord bridge (Phase 1).
 *
 * Discord POSTs every interaction here; we Ed25519-verify, then route. The
 * `/insights` command (and its period buttons) defers, then edits the original
 * message with the freshly-pulled Buffer digest. Also lays the groundwork for
 * Phase 2 buttons/modals. No always-on socket / privileged intents — a plain
 * node:http server, deployable as a small droplet service behind the registered
 * Interactions URL.
 *
 * Run: `pnpm --filter @grove/discord-bridge start`
 */
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { BufferClient } from "./lib/buffer.ts";
import { loadConfig } from "./lib/config.ts";
import { generateDigest } from "./lib/insights.ts";
import {
  editInteractionOriginal,
} from "./lib/discord.ts";
import { routeInteraction, type DiscordInteraction } from "./lib/interactions.ts";
import { renderDigestMessage } from "./lib/render.ts";
import { verifyDiscordRequest, ed25519PublicKey } from "./lib/verify.ts";
import { buildAuditEvent, emitAuditEvent } from "./lib/audit.ts";

const PORT = Number(process.env.PORT ?? 8787);
const BUFFER_ANALYTICS_URL = process.env.BUFFER_ANALYTICS_URL || "https://publish.buffer.com";

const cfg = loadConfig();
const publicKey = ed25519PublicKey(cfg.discordPublicKey);
const buffer = new BufferClient(cfg.bufferToken, cfg.bufferOrgId);

function readRawBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function json(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(payload);
}

async function handleInteractions(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const raw = await readRawBody(req);
  const sig = req.headers["x-signature-ed25519"] as string | undefined;
  const ts = req.headers["x-signature-timestamp"] as string | undefined;

  if (!verifyDiscordRequest(publicKey, sig, ts, raw)) {
    json(res, 401, { error: "invalid request signature" });
    return;
  }

  let interaction: DiscordInteraction;
  try {
    interaction = JSON.parse(raw) as DiscordInteraction;
  } catch {
    json(res, 400, { error: "invalid JSON" });
    return;
  }

  const result = routeInteraction(interaction);
  // Reply immediately (PONG / deferred / ephemeral error) — Discord's 3s budget.
  json(res, 200, result.response);

  if (!result.followup) return;

  // Deferred: pull the digest and edit the original message. Fire-and-forget;
  // errors are logged, not surfaced to the (already-answered) request.
  const { period } = result.followup;
  const token = (interaction as { token?: string }).token;
  const actor =
    (interaction as { member?: { user?: { id?: string } }; user?: { id?: string } }).member?.user
      ?.id ??
    (interaction as { user?: { id?: string } }).user?.id ??
    "unknown";

  void (async () => {
    try {
      const digest = await generateDigest(buffer, period);
      const message = renderDigestMessage(digest, period, BUFFER_ANALYTICS_URL);
      if (token) await editInteractionOriginal(cfg.discordAppId, token, message);
      emitAuditEvent(
        buildAuditEvent({ action: "digest_pull", actor, ts: new Date().toISOString(), period }),
      );
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("discord-bridge: /insights follow-up failed:", err);
    }
  })();
}

const server = createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    json(res, 200, { ok: true, service: "discord-bridge" });
    return;
  }
  if (req.method === "POST" && req.url === "/interactions") {
    handleInteractions(req, res).catch((err) => {
      // eslint-disable-next-line no-console
      console.error("discord-bridge: interaction handler error:", err);
      if (!res.headersSent) json(res, 500, { error: "internal error" });
    });
    return;
  }
  json(res, 404, { error: "not found" });
});

server.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`discord-bridge interactions server listening on :${PORT}`);
});
