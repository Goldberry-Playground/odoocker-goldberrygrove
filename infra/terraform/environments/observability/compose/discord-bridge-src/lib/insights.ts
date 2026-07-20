/**
 * Orchestrates a full digest pull: channels → per-channel metrics (current +
 * prior window) → top posts → assembled `weekly_digest` payload.
 */
import { BufferClient } from "./buffer.ts";
import { buildWeeklyDigest } from "./digest.ts";
import { engagements as sumEngagements, impressions as pickImpressions, postCount } from "./metrics.ts";
import { resolveWindow } from "./period.ts";
import type { ChannelStats, Period, WeeklyDigest } from "./types.ts";

/**
 * Services included in the CMO digest (GOL-233 §1a). Google Business is a
 * listing, not a content channel — excluded by default.
 */
export const DEFAULT_DIGEST_SERVICES = new Set(["threads", "instagram", "facebook", "youtube"]);

export interface GenerateOptions {
  services?: Set<string>;
  now?: Date;
}

export async function generateDigest(
  client: BufferClient,
  period: Period,
  opts: GenerateOptions = {},
): Promise<WeeklyDigest> {
  const now = opts.now ?? new Date();
  const services = opts.services ?? DEFAULT_DIGEST_SERVICES;
  const window = resolveWindow(period, now);

  const channels = (await client.listChannels()).filter((c) => services.has(c.service));

  const stats: ChannelStats[] = await Promise.all(
    channels.map(async (ch): Promise<ChannelStats> => {
      const [current, prior, topPost] = await Promise.all([
        client.aggregatedMetrics(ch.id, window.start, window.end),
        client.aggregatedMetrics(ch.id, window.prevStart, window.prevEnd),
        client.topPost(ch.id, window.start, window.end),
      ]);
      return {
        channel: ch.service,
        posts: postCount(current),
        impressions: pickImpressions(current),
        engagements: sumEngagements(current),
        prevEngagements: sumEngagements(prior),
        topPost,
      };
    }),
  );

  return buildWeeklyDigest(stats, window, now);
}
