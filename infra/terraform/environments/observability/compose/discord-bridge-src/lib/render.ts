/**
 * Renders a `weekly_digest` payload into a Discord message (one embed + a row
 * of period/Buffer buttons), per GOL-233 §1c:
 *   "one embed, headline as title, a compact per-channel table, then
 *    Best/Watch/Notes lines. Buttons: 📈 Last 30d · 📊 Last 90d · 🔗 Open in Buffer."
 */
import { serviceLabel } from "./digest.ts";
import { PERIODS } from "./period.ts";
import type { Period, WeeklyDigest } from "./types.ts";

/** Grove green (embed accent). */
const GROVE_GREEN = 0x3f7d4e;

const PERIOD_BUTTON: Record<Period, { emoji: string; label: string }> = {
  last7d: { emoji: "🗓️", label: "Last 7d" },
  last30d: { emoji: "📈", label: "Last 30d" },
  last90d: { emoji: "📊", label: "Last 90d" },
};

const BUFFER_ANALYTICS_URL_DEFAULT = "https://publish.buffer.com";

export interface DiscordEmbed {
  title: string;
  description: string;
  color: number;
  footer: { text: string };
}

export interface DiscordButton {
  type: 2;
  style: 1 | 2 | 3 | 4 | 5;
  label: string;
  emoji?: { name: string };
  custom_id?: string;
  url?: string;
}

export interface DiscordActionRow {
  type: 1;
  components: DiscordButton[];
}

export interface DiscordMessage {
  embeds: DiscordEmbed[];
  components: DiscordActionRow[];
}

function pct(n: number | null, digits = 1): string {
  if (n === null) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

function signedPct(n: number | null): string {
  if (n === null) return "—";
  const r = Math.round(n);
  return `${r >= 0 ? "+" : ""}${r}%`;
}

function fmt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

function pad(s: string, width: number, right = false): string {
  if (s.length >= width) return s;
  const fill = " ".repeat(width - s.length);
  return right ? fill + s : s + fill;
}

/** Compact monospace per-channel table for the embed description. */
export function renderTable(digest: WeeklyDigest): string {
  if (digest.channels.length === 0) return "```\n(no activity this period)\n```";
  const header = `${pad("#", 2)} ${pad("Channel", 10)} ${pad("Posts", 5, true)} ${pad(
    "Views",
    7,
    true,
  )} ${pad("Eng", 6, true)} ${pad("Rate", 5, true)} ${pad("WoW", 5, true)}`;
  const rows = digest.channels.map((c) =>
    `${pad(String(c.rank), 2)} ${pad(serviceLabel(c.channel), 10)} ${pad(fmt(c.posts), 5, true)} ` +
    `${pad(fmt(c.impressions), 7, true)} ${pad(fmt(c.engagements), 6, true)} ` +
    `${pad(pct(c.engagement_rate), 5, true)} ${pad(signedPct(c.wow_delta_pct), 5, true)}`,
  );
  return "```\n" + [header, ...rows].join("\n") + "\n```";
}

export function renderDigestMessage(
  digest: WeeklyDigest,
  currentPeriod: Period,
  bufferAnalyticsUrl: string = BUFFER_ANALYTICS_URL_DEFAULT,
): DiscordMessage {
  const lines: string[] = [renderTable(digest)];

  if (digest.best_content) {
    lines.push(
      `**🏆 Best content** ([${serviceLabel(digest.best_content.channel)}](${digest.best_content.permalink})) — ${digest.best_content.why}`,
    );
  }
  if (digest.watch_item) {
    lines.push(`**👀 Watch** — ${digest.watch_item}`);
  }
  lines.push(`_${digest.notes}_`);

  const embed: DiscordEmbed = {
    title: digest.headline,
    description: lines.join("\n\n"),
    color: GROVE_GREEN,
    footer: {
      text: `Period ${digest.period} · read-only insights · no auto-publish · generated ${digest.generated_at}`,
    },
  };

  // Offer the two periods other than the current one, then Open in Buffer.
  const alternatives = PERIODS.filter((p) => p !== currentPeriod);
  const buttons: DiscordButton[] = alternatives.map((p) => ({
    type: 2,
    style: 2,
    label: PERIOD_BUTTON[p].label,
    emoji: { name: PERIOD_BUTTON[p].emoji },
    custom_id: `insights:${p}`,
  }));
  buttons.push({
    type: 2,
    style: 5,
    label: "Open in Buffer",
    emoji: { name: "🔗" },
    url: bufferAnalyticsUrl,
  });

  return { embeds: [embed], components: [{ type: 1, components: buttons }] };
}
