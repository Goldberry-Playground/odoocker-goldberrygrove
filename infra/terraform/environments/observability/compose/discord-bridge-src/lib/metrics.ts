/**
 * Pure metric-parsing helpers. Buffer returns a flat list of typed metrics
 * (`{ type, value }`) per channel; different services expose different types
 * (Threads has reactions/comments/quotes/reposts/views; YouTube/IG differ).
 * These helpers normalize any service's list into the digest's canonical
 * `posts / impressions / engagements` triad (GOL-233 §1a).
 */
import type { BufferMetric } from "./types.ts";

/**
 * Metric types that count as an "engagement". Buffer's own `engagementRate`
 * is intentionally excluded — the digest recomputes rate as
 * engagements/impressions per the spec. `postCount`, `views`, `impressions`,
 * `reach`, `engagementRate` are never engagements.
 */
const ENGAGEMENT_TYPES = new Set([
  "reactions",
  "likes",
  "comments",
  "replies",
  "reposts",
  "retweets",
  "shares",
  "quotes",
  "saves",
  "bookmarks",
  "clicks",
]);

/** Types that represent "impressions", in priority order (first present wins). */
const IMPRESSION_TYPES = ["impressions", "views", "reach"];

function valueOf(metrics: BufferMetric[], type: string): number | undefined {
  const m = metrics.find((x) => x.type === type);
  return m ? m.value : undefined;
}

/** Post count for the window (Buffer `postCount`). */
export function postCount(metrics: BufferMetric[]): number {
  return valueOf(metrics, "postCount") ?? 0;
}

/** Best-available impressions figure for the window. */
export function impressions(metrics: BufferMetric[]): number {
  for (const t of IMPRESSION_TYPES) {
    const v = valueOf(metrics, t);
    if (v !== undefined) return v;
  }
  return 0;
}

/** Sum of every engagement-type metric present. */
export function engagements(metrics: BufferMetric[]): number {
  return metrics
    .filter((m) => ENGAGEMENT_TYPES.has(m.type))
    .reduce((sum, m) => sum + (Number.isFinite(m.value) ? m.value : 0), 0);
}

/** engagements / impressions, or null when impressions === 0 (GOL-233 §1a). */
export function engagementRate(eng: number, impr: number): number | null {
  if (!impr) return null;
  return eng / impr;
}

/**
 * Week-over-week delta as a percentage. null when there is no prior data or
 * the prior window had zero engagements (avoid divide-by-zero / infinite %).
 */
export function wowDeltaPct(current: number, prior: number | null): number | null {
  if (prior === null || prior === 0) return null;
  return ((current - prior) / prior) * 100;
}
