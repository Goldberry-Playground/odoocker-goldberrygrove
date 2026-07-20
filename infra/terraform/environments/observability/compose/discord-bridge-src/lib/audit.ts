/**
 * Audit trail (GOL-233 §5b). Every digest pull / (Phase 2) approve-revise-reject
 * writes an immutable `evt_` record. Phase 1 emits `digest_pull` events; the
 * Paperclip-comment mirror is wired in Phase 2 (GOL-259). For now the sink is
 * structured stdout so the events are captured in run/droplet logs.
 */
export type AuditAction = "digest_pull" | "digest_scheduled";

export interface AuditEventInput {
  action: AuditAction;
  /** Discord user id, or "scheduler" for the cron-driven weekly digest. */
  actor: string;
  ts: string;
  period?: string;
  discord_message_link?: string;
  reason?: string;
}

export interface AuditEvent extends AuditEventInput {
  event_id: string;
  publish_mode: "draft_only";
}

/** Build an immutable audit record. `publish_mode` is pinned to draft_only in Phase 1. */
export function buildAuditEvent(input: AuditEventInput): AuditEvent {
  const stamp = input.ts.replace(/[^0-9]/g, "").slice(0, 14);
  return {
    event_id: `evt_${stamp}_${input.action}`,
    publish_mode: "draft_only",
    ...input,
  };
}

/** Emit an audit event to the structured-log sink. */
export function emitAuditEvent(event: AuditEvent): void {
  // eslint-disable-next-line no-console
  console.log(`AUDIT ${JSON.stringify(event)}`);
}
