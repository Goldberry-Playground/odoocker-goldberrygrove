/**
 * Ed25519 verification of Discord HTTP interaction requests.
 *
 * Discord signs every interaction POST; the receiver MUST verify or Discord
 * rejects the endpoint (and unverified requests must get a 401). Implemented
 * with Node's built-in `crypto` (Node 22+) — no external deps (no tweetnacl /
 * discord-interactions). Discord's public key is a raw 32-byte Ed25519 key in
 * hex; we wrap it in an SPKI DER header so `createPublicKey` accepts it.
 */
import { createPublicKey, verify as cryptoVerify, type KeyObject } from "node:crypto";

/** DER SPKI header for a raw Ed25519 public key (RFC 8410), 12 bytes. */
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");

function isHex(s: string): boolean {
  return /^[0-9a-fA-F]+$/.test(s) && s.length % 2 === 0;
}

/** Build a Node KeyObject from a raw 32-byte Ed25519 public key (hex). */
export function ed25519PublicKey(publicKeyHex: string): KeyObject {
  if (!isHex(publicKeyHex) || publicKeyHex.length !== 64) {
    throw new Error("Invalid Discord public key: expected 32-byte hex string");
  }
  const der = Buffer.concat([ED25519_SPKI_PREFIX, Buffer.from(publicKeyHex, "hex")]);
  return createPublicKey({ key: der, format: "der", type: "spki" });
}

/**
 * Verify a Discord interaction request.
 * @param publicKey  KeyObject (from {@link ed25519PublicKey}) or raw hex key.
 * @param signature  `X-Signature-Ed25519` header (hex).
 * @param timestamp  `X-Signature-Timestamp` header.
 * @param rawBody    The exact raw request body bytes/string (pre-JSON.parse).
 */
export function verifyDiscordRequest(
  publicKey: KeyObject | string,
  signature: string | undefined,
  timestamp: string | undefined,
  rawBody: string | Buffer,
): boolean {
  if (!signature || !timestamp || !isHex(signature)) return false;
  try {
    const key = typeof publicKey === "string" ? ed25519PublicKey(publicKey) : publicKey;
    const message = Buffer.concat([
      Buffer.from(timestamp, "utf8"),
      typeof rawBody === "string" ? Buffer.from(rawBody, "utf8") : rawBody,
    ]);
    return cryptoVerify(null, message, key, Buffer.from(signature, "hex"));
  } catch {
    return false;
  }
}
