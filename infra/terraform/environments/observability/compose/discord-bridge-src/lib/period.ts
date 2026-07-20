/**
 * Reporting-window math. Windows are whole UTC days, inclusive, and each
 * carries the immediately-prior equal-length window so the digest can compute
 * week-over-week (WoW) deltas (GOL-233 §1a `wow_delta`).
 */
import type { Period, PeriodWindow } from "./types.ts";

const DAYS: Record<Period, number> = {
  last7d: 7,
  last30d: 30,
  last90d: 90,
};

export const PERIODS: Period[] = ["last7d", "last30d", "last90d"];

/** Parse/normalize a user-supplied period string, defaulting to last7d. */
export function parsePeriod(raw: string | null | undefined): Period {
  const v = (raw ?? "").trim().toLowerCase();
  if (v === "7d" || v === "last7d" || v === "week") return "last7d";
  if (v === "30d" || v === "last30d" || v === "month") return "last30d";
  if (v === "90d" || v === "last90d" || v === "quarter") return "last90d";
  return "last7d";
}

function ymd(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Midnight (00:00:00.000Z) at the start of `d`'s UTC day. */
function startOfUtcDay(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(), 0, 0, 0, 0));
}

/**
 * Resolve the current + prior windows for `period`. `now` is injectable so the
 * math is deterministically testable (production passes `new Date()`).
 */
export function resolveWindow(period: Period, now: Date): PeriodWindow {
  const days = DAYS[period];
  // End = end of today (UTC). Start = midnight `days-1` days ago → `days` whole
  // inclusive days.
  const startDay = startOfUtcDay(new Date(now.getTime() - (days - 1) * 86_400_000));
  const start = startDay;
  const end = new Date(startOfUtcDay(now).getTime() + 86_400_000 - 1); // 23:59:59.999 today

  const prevEnd = new Date(start.getTime() - 1); // 1ms before current window
  const prevStart = new Date(start.getTime() - days * 86_400_000);

  return {
    period,
    start: start.toISOString(),
    end: end.toISOString(),
    prevStart: prevStart.toISOString(),
    prevEnd: prevEnd.toISOString(),
    label: `${ymd(start)}..${ymd(new Date(now))}`,
  };
}
