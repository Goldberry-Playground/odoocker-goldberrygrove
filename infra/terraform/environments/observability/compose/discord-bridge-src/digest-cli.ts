/**
 * Scheduled weekly Buffer digest (Phase 1).
 *
 * Invoked Mondays 08:00 ET by the GitHub Actions cron (or a droplet cron):
 * pulls the last-7d digest and posts it to #weekly-insights. Accepts an
 * optional period arg for manual/on-demand runs, e.g.:
 *   `pnpm --filter @grove/discord-bridge digest -- last30d`
 */
import { BufferClient } from "./lib/buffer.ts";
import { loadConfig } from "./lib/config.ts";
import { generateDigest } from "./lib/insights.ts";
import { postChannelMessage } from "./lib/discord.ts";
import { renderDigestMessage } from "./lib/render.ts";
import { parsePeriod } from "./lib/period.ts";
import { buildAuditEvent, emitAuditEvent } from "./lib/audit.ts";

const BUFFER_ANALYTICS_URL = process.env.BUFFER_ANALYTICS_URL || "https://publish.buffer.com";

async function main(): Promise<void> {
  const period = parsePeriod(process.argv[2] ?? "last7d");
  const cfg = loadConfig();
  const buffer = new BufferClient(cfg.bufferToken, cfg.bufferOrgId);

  const digest = await generateDigest(buffer, period);
  const message = renderDigestMessage(digest, period, BUFFER_ANALYTICS_URL);
  await postChannelMessage(cfg.discordBotToken, cfg.weeklyInsightsChannelId, message);

  emitAuditEvent(
    buildAuditEvent({ action: "digest_scheduled", actor: "scheduler", ts: new Date().toISOString(), period }),
  );
  // eslint-disable-next-line no-console
  console.log(`discord-bridge: posted ${period} digest — ${digest.headline}`);
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error("discord-bridge: digest run failed:", err);
  process.exit(1);
});
