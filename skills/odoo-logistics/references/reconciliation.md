# Stripe / Shippo ↔ Odoo reconciliation surface (draft)

Scope for reconciling external payment (Stripe) and shipping (Shippo) records
against Odoo. **Draft — pending confirmation from CFO (Penny) on GOL-57** for
the exact data set and any additional external read access she wants wired in.

Everything below is **read-only** through the `odoo-logistics` skill.

## The Odoo side (available now)

| Purpose                         | Model / fields                                                        |
| ------------------------------- | -------------------------------------------------------------------- |
| Sales orders (what was sold)    | `sale.order`: name, partner_id, amount_total, state, date_order      |
| Order lines                     | `sale.order.line`: product_id, product_uom_qty, price_subtotal       |
| Customer invoices / credit      | `account.move` (move_type in invoice/refund): name, amount_total, ref |
| Payments                        | `account.payment`: amount, payment_method_line_id, ref, date         |
| Payment provider transactions   | `payment.transaction`: reference, provider_reference, amount, state  |
| Deliveries (what shipped)       | `stock.picking` (outgoing): name, carrier_id, carrier_tracking_ref   |
| Carriers                        | `delivery.carrier`: name, delivery_type                              |

`payment.transaction.provider_reference` is the join key to Stripe charge/
payment-intent ids; `stock.picking.carrier_tracking_ref` is the join key to
Shippo tracking numbers.

## The external side (needs scope + creds — GOL-57 follow-up)

Reconciling *against* Stripe and Shippo requires read access to those APIs
(Stripe payments/balance transactions; Shippo transactions/tracking). Those are
separate secrets and are **not** part of this Odoo skill. Track their scope and
credential injection with CFO (Penny) as a follow-up; do not embed those keys in
agent config either.

## Open questions for Penny (CFO)

1. Which cadence / period does reconciliation run on (per-order, daily, weekly)?
2. Is the reconciliation authoritative source Odoo, Stripe, or a spreadsheet
   today — i.e. what are we reconciling *to*?
3. Does Otto need Stripe/Shippo **API** read access, or is exported CSV/Odoo
   data enough to start?
4. Any fields beyond the table above needed to match rows end-to-end?
