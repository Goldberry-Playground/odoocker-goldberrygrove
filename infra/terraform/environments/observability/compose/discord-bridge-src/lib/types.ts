/**
 * Shared types for the Discord bridge.
 *
 * The `WeeklyDigest` shape is the load-bearing CMO payload contract frozen in
 * GOL-233 §1c (Sora, CMO). Field names here MUST match that spec — the digest
 * is consumed downstream (audit mirror, Phase 2 analysis) by field name.
 */

/** Supported on-demand / scheduled reporting windows. */
export type Period = "last7d" | "last30d" | "last90d";

/** A resolved reporting window plus the immediately-prior equal-length window. */
export interface PeriodWindow {
  period: Period;
  /** Inclusive ISO-8601 start of the current window (UTC). */
  start: string;
  /** Inclusive ISO-8601 end of the current window (UTC). */
  end: string;
  /** ISO-8601 start of the prior equal-length window (for WoW deltas). */
  prevStart: string;
  /** ISO-8601 end of the prior equal-length window. */
  prevEnd: string;
  /** Human label, e.g. "2026-07-01..2026-07-07". */
  label: string;
}

/** One Buffer channel (social account). */
export interface BufferChannel {
  id: string;
  /** Buffer service key, e.g. "threads" | "facebook" | "instagram" | "youtube". */
  service: string;
  name: string;
}

/** A single metric datum from Buffer's `aggregatedPostMetrics` / `Post.metrics`. */
export interface BufferMetric {
  type: string;
  value: number;
  name?: string;
  unit?: string;
}

/** The best-performing post for a channel over the window (GOL-233 §1a `top_post`). */
export interface TopPost {
  text_preview: string;
  permalink: string;
  published_at: string;
  impressions: number;
  engagements: number;
}

/** Per-channel roll-up in the digest (GOL-233 §1a). Field names are load-bearing. */
export interface ChannelDigest {
  channel: string;
  rank: number;
  posts: number;
  impressions: number;
  engagements: number;
  /** engagements / impressions, or null when impressions === 0. */
  engagement_rate: number | null;
  /** % change in engagements vs the prior equal window, or null with no prior data. */
  wow_delta_pct: number | null;
  top_post: TopPost | null;
}

/** The full weekly-digest payload (GOL-233 §1c). */
export interface WeeklyDigest {
  type: "weekly_digest";
  /** e.g. "2026-07-01..2026-07-07" */
  period: string;
  generated_at: string;
  headline: string;
  channels: ChannelDigest[];
  best_content: { channel: string; permalink: string; why: string } | null;
  watch_item: string | null;
  notes: string;
}

/** Intermediate per-channel stats before ranking/summary (internal). */
export interface ChannelStats {
  channel: string;
  posts: number;
  impressions: number;
  engagements: number;
  prevEngagements: number | null;
  topPost: TopPost | null;
}
