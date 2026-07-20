/**
 * One-time (idempotent) registration of the `/insights` slash command with
 * Discord. Re-run whenever the command definition changes.
 *   `pnpm --filter @grove/discord-bridge register-commands`
 */
import { loadConfig } from "./lib/config.ts";
import { registerGlobalCommands, INSIGHTS_COMMAND } from "./lib/discord.ts";

async function main(): Promise<void> {
  const cfg = loadConfig();
  await registerGlobalCommands(cfg.discordBotToken, cfg.discordAppId, [INSIGHTS_COMMAND]);
  // eslint-disable-next-line no-console
  console.log("discord-bridge: registered /insights command.");
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error("discord-bridge: command registration failed:", err);
  process.exit(1);
});
