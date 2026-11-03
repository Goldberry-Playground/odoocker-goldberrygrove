# Synthetic Tier-1 journey: cart add → cart get for one tenant.
# Exercises the BFF↔Odoo write path that the read journeys can't: POST adds a
# line to a session cart (website_sale), then GET must reflect it. Hurl
# preserves the session cookie across entries in this file, so the GET sees the
# same cart the POST created.
# Catches: BFF↔Odoo session break, cart proxy / company-scoping regressions.
# Side effect: a cart POST with no session creates a draft sale.order on the
# website's Public user, and Odoo does NOT expire it — each one also burns a
# number from the shared sale.order sequence. So run.py replays a per-tenant,
# per-UTC-day cookie jar: every run lands in the same session cart (quantity
# grows by 1 per run) and only one draft per tenant per day is created. Old
# carts are deleted by grove_headless's abandoned-cart autovacuum (1-day TTL).

GET {{odoo_base}}/grove/api/v1/products
X-Grove-Tenant: {{tenant}}
HTTP 200
[Captures]
product_id: jsonpath "$.results[0].id"

POST {{odoo_base}}/grove/api/v1/cart
X-Grove-Tenant: {{tenant}}
Content-Type: application/json
{
  "product_id": {{product_id}},
  "quantity": 1
}
HTTP 200
[Asserts]
jsonpath "$.id" isInteger
jsonpath "$.lines" count > 0
jsonpath "$.lines[0].product_id" exists
jsonpath "$.lines[0].quantity" >= 1

GET {{odoo_base}}/grove/api/v1/cart
X-Grove-Tenant: {{tenant}}
HTTP 200
[Asserts]
jsonpath "$.lines" count > 0
