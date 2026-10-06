# Design decisions

## Why Zoho Inventory

- Real OAuth 2.0 with refresh tokens, so the auth work is not mocked.
- Real rate limits and real 429s (code 44 per minute, code 45 per day), so the limiter is tested against something that exists.
- Sales orders and inventory items give two related read use cases: "where is my order" and "do we have stock".

## Why read-only

- A first agent should not be able to change business data: wrong guesses cost nothing.
- Read-only scopes (`*.READ`) mean even a leaked token cannot write.
- Smaller surface: seven tools, no confirmation flows, nothing to undo.
- Enforced by tests: `ZohoClient` issues only GET, and only it calls the Inventory API.

## Why MCP, with both transports

- MCP provides a standard protocol for an agent platform to discover tools from their schemas and descriptions.
- stdio is the simplest local setup and works with the MCP Inspector.
- Streamable HTTP (`/mcp`) lets the connector run as a separate service; a bearer token (`MCP_SERVER_TOKEN`) protects it and it binds to localhost by default.
- Tool descriptions say when to use each tool and when to use a sibling instead, to reduce wrong tool picks.

## Why a daily budget of 900 and a bucket of 90/min

- Zoho's free plan allows 1000/day and 100/min per organization. The connector stops 10% short on both.
- The gap provides headroom for retries, clock skew, and other API consumers using the same organization.
- The budget is persisted (`tokens/usage.json`) so a restart does not reset the count.
- At most 4 requests in flight, one below Zoho's limit of 5 concurrent.
- Caching (60 s, 300 s for item detail) cuts repeat calls before they cost budget.

## Why fixtures and demo mode

- A reviewer can run everything without a Zoho account or credentials.
- Tests are fast, deterministic and cost no quota.
- Fixtures are captured from real responses (`scripts/capture_fixtures.py`) and go through the real `ZohoClient`, so demo mode exercises the same cache, limiter and mapping code.

## Why small output models

- Unknown Zoho fields are dropped on purpose: less data to leak, fewer tokens for the agent.
- Notes and other free text are untrusted and could carry prompt injection, so they are left out.
- Contact details are masked before they leave the connector.

## What I would do with more time

- **OpenAPI wrapper.** Generate or describe the Zoho calls from an OpenAPI spec instead of hand-written tools.
- **Webhooks for stock changes.** Invalidate the cache when stock changes, so the 5-minute staleness goes away and fewer polling calls are needed.
- **Per-user OAuth for multi-tenant use.** The current connector stores one authenticated Zoho connection and operates against one configured organization at a time.
- **Redis-backed limiter.** The bucket, budget and cache live in one process; several instances would each spend the full quota.
