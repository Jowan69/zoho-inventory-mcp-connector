# Capabilities and limits

## The agent can

- Find a sales order by order number, reference number or customer name.
- Show one sales order with its line items (SKU, name, quantity, rate, amount) and status.
- Browse sales orders by status (draft, confirmed, fulfilled, void) and by date range.
- Search items by name or SKU and show stock on hand and available stock.
- Show one item with its reorder level and stock per location.
- List items by status: active, inactive, all or low stock.
- Page through long results with `next_page`.
- Report connector mode, login state, organization and how much of today's request budget is used (`connector_status`).

## The agent cannot

- **Write anything.** No create, update, cancel or delete. Zoho is only ever called with GET and read-only scopes.
- **Reach other modules.** No invoices, payments, purchase orders, contacts or other Zoho Inventory modules.
- **Switch organizations.** The connector operates against one configured organization at a time: the organization selected during login, or the one specified by `ZOHO_ORG_ID`.
- **See fresh stock instantly.** Responses are cached for 60 s, and item detail (stock per location) for up to 300 s, so stock can be up to 5 minutes old.
- **Make unlimited calls.** The connector enforces its own `DAILY_BUDGET` per UTC day, below Zoho's Free-plan API limit.
- **Run analytics.** No totals, trends or reports are computed by the connector. The `lowstock` list uses Zoho's filter; the agent can also compare returned stock and reorder-level values when answering a question.
- **Read free text.** Notes, terms and descriptions are not returned.

## Data and safety

- Orders return number, date, customer name, status, total, currency, shipment status and line items.
- Customer email and phone are masked: `j***@example.com` and `***1234`.
- Tokens and the client secret never appear in logs or tool output.
- Errors are sanitized and capped at 300 characters

## When the agent must stop

| Error code | What the agent tells the user |
|---|---|
| `AUTH_REQUIRED` | The Zoho connection is not logged in. An admin must run `auth login`. Stop. |
| `DAILY_QUOTA_EXHAUSTED` | The daily request limit is used up. It resets at 00:00 UTC. Stop. |
| `RATE_LIMITED` | Zoho is busy. Wait about a minute and retry once; if it fails again, tell the user. |
| `UPSTREAM_ERROR` | Zoho is failing. Retry once later; if it fails again, tell the user Zoho is failing. |
| `NOT_FOUND` | That ID was not found. Ask the user to check it, or search instead. |
| `INVALID_INPUT` | The request was malformed. Fix the arguments as the message says and call again. |

Any request to change data (for example "cancel order SO-00011") must be refused: the connector is read only.
