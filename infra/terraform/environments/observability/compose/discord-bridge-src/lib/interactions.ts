/**
 * Routes a verified Discord interaction to an immediate response, plus an
 * optional deferred follow-up (the digest pull, which exceeds Discord's 3s
 * synchronous budget). Pure/synchronous so it is trivially unit-testable; the
 * server performs the async follow-up work.
 */
import { parsePeriod } from "./period.ts";
import type { Period } from "./types.ts";

/** Discord interaction request types. */
export const InteractionType = {
  PING: 1,
  APPLICATION_COMMAND: 2,
  MESSAGE_COMPONENT: 3,
} as const;

/** Discord interaction response types. */
export const ResponseType = {
  PONG: 1,
  CHANNEL_MESSAGE_WITH_SOURCE: 4,
  DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE: 5,
  DEFERRED_UPDATE_MESSAGE: 6,
} as const;

/** Ephemeral message flag. */
const EPHEMERAL = 64;

export const INSIGHTS_COMMAND_NAME = "insights";

export interface DiscordInteraction {
  type: number;
  data?: {
    name?: string;
    custom_id?: string;
    options?: Array<{ name: string; value?: unknown }>;
  };
}

export interface InteractionResult {
  /** JSON to return synchronously to Discord. */
  response: { type: number; data?: Record<string, unknown> };
  /** When set, the server must generate the digest and edit the original message. */
  followup?: { period: Period };
}

function optionValue(interaction: DiscordInteraction, name: string): string | undefined {
  const opt = interaction.data?.options?.find((o) => o.name === name);
  return typeof opt?.value === "string" ? opt.value : undefined;
}

/**
 * Route an interaction whose Ed25519 signature has ALREADY been verified.
 * Returns the immediate response + any deferred follow-up.
 */
export function routeInteraction(interaction: DiscordInteraction): InteractionResult {
  if (interaction.type === InteractionType.PING) {
    return { response: { type: ResponseType.PONG } };
  }

  if (interaction.type === InteractionType.APPLICATION_COMMAND) {
    if (interaction.data?.name === INSIGHTS_COMMAND_NAME) {
      const period = parsePeriod(optionValue(interaction, "period"));
      // Defer: the digest pull round-trips Buffer, exceeding the 3s budget.
      return { response: { type: ResponseType.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE }, followup: { period } };
    }
    return unknown(`Unknown command: /${interaction.data?.name ?? "?"}`);
  }

  if (interaction.type === InteractionType.MESSAGE_COMPONENT) {
    const customId = interaction.data?.custom_id ?? "";
    if (customId.startsWith("insights:")) {
      const period = parsePeriod(customId.slice("insights:".length));
      // Edit the existing digest message in place once regenerated.
      return { response: { type: ResponseType.DEFERRED_UPDATE_MESSAGE }, followup: { period } };
    }
    return unknown(`Unknown action: ${customId}`);
  }

  return unknown("Unsupported interaction type");
}

function unknown(message: string): InteractionResult {
  return {
    response: {
      type: ResponseType.CHANNEL_MESSAGE_WITH_SOURCE,
      data: { content: message, flags: EPHEMERAL },
    },
  };
}
