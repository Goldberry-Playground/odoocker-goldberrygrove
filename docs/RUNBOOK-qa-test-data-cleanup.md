# Runbook — QA test-data cleanup (pre-freeze teardown)

Tear the QA env's **test data** back down to a clean, reproducible state before
a freeze, without touching the **real** order/inventory data QA has held as the
Grove system of record since 2026-07-09.

- Script: `scripts/qa-test-data-cleanup.py` (worker) + `scripts/qa-test-data-cleanup.sh` (entrypoint)
- Tests: `scripts/test_qa_test_data_cleanup.py` (`python3 scripts/test_qa_test_data_cleanup.py`)
- Make: `make qa-test-data-cleanup` (dry-run) · `make qa-test-data-cleanup-apply` (delete)

## What counts as "test data" (and why it's safe to delete)

QA accumulates test records over a release cycle:

- synthetic-monitoring **canary orders** (`synthetic/canary.py`, checkout-canary journey),
- **cart-flow** journey draft carts,
- the **SYNTHETIC-CANARY** product,
- any **partners/orders** seeded by hand while QAing.

Every one of those fixtures uses an email at an **RFC 2606 / RFC 6761 reserved
test domain** — `.invalid`, `.test`, `.example`, `.localhost`, or
`example.{com,net,org}` (e.g. `synthetic-canary@grove.invalid`). The DNS root
will never delegate those TLDs, so **no real customer can ever own an address
there.** That makes a reserved-domain email a zero-false-positive marker for
test data. The cleanup deletes **only** on that marker (plus the well-known
`SYNTHETIC-CANARY` product code) — never on dates, amounts, or order state.

Deletes go through Odoo's **ORM over XML-RPC**, never raw SQL, so FK cascades,
access rules, and record rules are respected — a delete that would corrupt
referential integrity is refused by Odoo, not silently orphaned.

## Safety rails

| Rail | Behaviour |
|---|---|
| **Dry-run default** | Prints what it *would* remove and exits. `--apply` is required to delete. |
| **DB-name guard** | Refuses unless `ODOO_DB` looks like a QA/sandbox/staging DB. Override with `--allow-nonqa-db` (loud warning) — do **not** point this at prod. |
| **Idempotent** | Selectors are stable; a second `--apply` removes 0 records. Safe to re-run. |
| **Canary product kept** | The `SYNTHETIC-CANARY` product is a persistent monitoring fixture (`setup-monitoring.py` re-seeds it), so it's kept unless you pass `--include-canary-product`. |
| **Anon carts report-only** | Anonymous website "abandoned cart" drafts are indistinguishable from a real shopper's, so they're only *counted*, never deleted — Odoo's own cron expires them. |

## Run it

### Credentials
Reuses the **canary XML-RPC user** already provisioned in QA Odoo
(`ODOO_DB` / `ODOO_LOGIN` / `SYNTHETIC_ODOO_API_KEY`, from the `Grove QA` vault).
Give that user delete rights on `sale.order` / `res.partner` (and
`product.template` only if you use `--include-canary-product`).

**(a) On the QA obs droplet** — the `synthetic` container's env already has the
three vars, so no `op` needed:

```bash
ODOO_XMLRPC_URL=http://odoo:8069 ODOO_DB=... ODOO_LOGIN=... \
  SYNTHETIC_ODOO_API_KEY=... bash scripts/qa-test-data-cleanup.sh          # dry-run
#                                                              ... --apply  # delete
```

**(b) Locally via 1Password** — fill the item id in
`scripts/qa-test-data-cleanup.env.op`, be `op` signed-in with `Grove QA` access:

```bash
QA_CLEANUP_ENV_OP=scripts/qa-test-data-cleanup.env.op \
  bash scripts/qa-test-data-cleanup.sh            # dry-run
QA_CLEANUP_ENV_OP=scripts/qa-test-data-cleanup.env.op \
  bash scripts/qa-test-data-cleanup.sh --apply    # delete
```

### Recommended pre-freeze sequence

1. **Dry-run** and eyeball the report (partner emails, order names/states). If
   anything without a reserved-domain email shows up, STOP — that's a bug, not
   test data.
2. `--apply` to remove test orders + partners.
3. Re-run dry-run → all counts should read `0` (idempotency = clean state).
4. (Optional) `--apply --include-canary-product` if you want the monitoring
   product gone too; note the next monitoring cycle re-seeds it.

## Flags

| Flag | Effect |
|---|---|
| *(none)* | dry-run report — deletes nothing |
| `--apply` | actually delete test orders + partners |
| `--include-canary-product` | also delete the `SYNTHETIC-CANARY` product |
| `--allow-nonqa-db` | bypass the QA-DB-name guard (dangerous) |
| `--json` | machine-readable summary on stdout (for CI) |

## Verify

- `scripts/test_qa_test_data_cleanup.py` proves surgical selection + idempotency
  against an in-memory Odoo (no network).
- Against live QA: dry-run → `--apply` → dry-run reads all-zero.
