/**
 * Builds the `weekly_digest` payload (GOL-233 §1b/§1c) from per-channel stats.
 *
 * Rules (load-bearing, from Sora's CMO spec):
 *  - Channels ranked DESCENDING by engagements; digest leads with the top one
 *    (structurally Threads, ~97% of engagement per GOL-226).
 *  - Headline names the dominant channel + its share of total engagement.
 *  - best_content = single highest-engagement post across all channels.
 *  - watch_item = one honest note (lead channel drops >20% WoW, or a channel
 *    posted 0). Positive-but-realistic — no doom, no spin.
 *  - ZERO demographic claims (Buffer exposes none — GOL-227).
 */
import { engagementRate, wowDeltaPct } from "./metrics.ts";
import type { ChannelDigest, ChannelStats, Period, PeriodWindow, WeeklyDigest } from "./types.ts";

const DEMOGRAPHIC_NOTE =
  "No demographic data available via Buffer (age/gender need Coupler — GOL-227).";

/** WoW drop threshold on the lead channel that earns a watch-item note. */
const WOW_DROP_THRESHOLD = -20;

const PERIOD_PHRASE: Record<Period, string> = {
  last7d: "this week",
  last30d: "in the last 30 days",
  last90d: "in the last 90 days",
};

export function serviceLabel(service: string): string {
  const map: Record<string, string> = {
    threads: "Threads",
    facebook: "Facebook",
    instagram: "Instagram",
    youtube: "YouTube",
    googlebusiness: "Google Business",
  };
  return map[service] ?? service.charAt(0).toUpperCase() + service.slice(1);
}

function fmt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

/** Infer a light "why it worked" theme hint from post copy — never overclaims. */
function inferWhy(text: string): string {
  const hashtags = text.match(/#[A-Za-z0-9_]+/g) ?? [];
  const tree = hashtags.find((h) => /treefacts/i.test(h));
  if (tree) return `${tree} — educational storytelling, our strongest pattern`;
  if (hashtags.length) return `${hashtags[0]} theme — strong engagement`;
  return "highest-engagement post of the period";
}

export function buildWeeklyDigest(
  stats: ChannelStats[],
  window: PeriodWindow,
  generatedAt: Date,
): WeeklyDigest {
  const active = stats.filter((s) => s.posts > 0 || s.engagements > 0);
  const ranked = [...active].sort((a, b) => b.engagements - a.engagements);
  const totalEngagements = ranked.reduce((sum, s) => sum + s.engagements, 0);

  const channels: ChannelDigest[] = ranked.map((s, i) => ({
    channel: s.channel,
    rank: i + 1,
    posts: s.posts,
    impressions: s.impressions,
    engagements: s.engagements,
    engagement_rate: engagementRate(s.engagements, s.impressions),
    wow_delta_pct: wowDeltaPct(s.engagements, s.prevEngagements),
    top_post: s.topPost,
  }));

  const lead = channels[0];
  const headline = lead
    ? `${serviceLabel(lead.channel)} did ${sharePct(lead.engagements, totalEngagements)}% of ` +
      `your engagement ${PERIOD_PHRASE[window.period]} — ${fmt(lead.impressions)} views across ` +
      `${fmt(lead.posts)} ${lead.posts === 1 ? "post" : "posts"}.`
    : `No posts published ${PERIOD_PHRASE[window.period]}.`;

  const best = pickBestContent(channels);
  const watch_item = buildWatchItem(lead, stats);

  return {
    type: "weekly_digest",
    period: window.label,
    generated_at: generatedAt.toISOString(),
    headline,
    channels,
    best_content: best,
    watch_item,
    notes: DEMOGRAPHIC_NOTE,
  };
}

function sharePct(part: number, total: number): number {
  if (!total) return 0;
  return Math.round((part / total) * 100);
}

function pickBestContent(channels: ChannelDigest[]): WeeklyDigest["best_content"] {
  let best: { channel: string; permalink: string; why: string } | null = null;
  let bestEng = -1;
  for (const c of channels) {
    if (c.top_post && c.top_post.engagements > bestEng && c.top_post.permalink) {
      bestEng = c.top_post.engagements;
      best = {
        channel: c.channel,
        permalink: c.top_post.permalink,
        why: inferWhy(c.top_post.text_preview),
      };
    }
  }
  return best;
}

function buildWatchItem(
  lead: ChannelDigest | undefined,
  allStats: ChannelStats[],
): string | null {
  const notes: string[] = [];
  if (lead && lead.wow_delta_pct !== null && lead.wow_delta_pct < WOW_DROP_THRESHOLD) {
    notes.push(
      `${serviceLabel(lead.channel)} engagement down ${Math.abs(Math.round(lead.wow_delta_pct))}% WoW.`,
    );
  }
  const silent = allStats.filter((s) => s.posts === 0).map((s) => serviceLabel(s.channel));
  if (silent.length) {
    notes.push(`${silent.join(", ")} posted 0 this period.`);
  }
  return notes.length ? notes.join(" ") : null;
}
