# Zoho Inventory MCP Connector - agent rules
## Goal
Read-only connector. An Agent Studio agent reads sales orders and inventory items from Zoho Inventory through MCP tools.
## Hard rules
- Read only. HTTP GET only against Zoho. Never add create/update/delete tools.
- Never run git commit, git push, or change git config. The human commits.
- Never read .env or tokens/. Never print, log or return tokens or secrets.
- All Zoho HTTP calls go through ZohoClient. No other module calls Zoho directly.
- Python 3.11+, async httpx, pydantic v2, full type hints, ruff clean.
- Before you say a task is done, run `uv run pytest -q` and `uv run ruff check .` and show the output.
- Stay inside the scope of the current task. Ask before adding a dependency.
## Zoho facts
- API base: https://www.zohoapis.{dc}/inventory/v1 ; accounts: https://accounts.zoho.{dc}
- Header: Authorization: Zoho-oauthtoken {access_token}. Query param organization_id on every call.
- Scopes: ZohoInventory.items.READ,ZohoInventory.salesorders.READ,ZohoInventory.settings.READ
- Access token ~1 h. Refresh token permanent (max 20 per user). Grant code valid 60 s.
- Free plan limits: 100 req/min/org, 1000 req/day, 5 concurrent. HTTP 429 with code 44 = per-minute, code 45 = daily.
- Pagination: page, per_page; response page_context.has_more_page.
## Connector limits
- Token bucket 90/min, concurrency semaphore 4, daily budget DAILY_BUDGET (default 900), warn at 80%.
- 429 code 44: use Retry-After, else min(2**attempt, 30) + random 0-1 s; max 3 retries. Code 45: no retry. 5xx: 1 retry after 2 s. 401: refresh token once, then retry once.
- Cache GET responses 60 s, item detail 300 s. Never cache errors.
## Error codes returned to the agent
NOT_FOUND, INVALID_INPUT, RATE_LIMITED, DAILY_QUOTA_EXHAUSTED, AUTH_REQUIRED, UPSTREAM_ERROR
## Layout
src/zoho_connector/{config.py, errors.py, models.py, cli.py, server.py, auth/, client/, tools/}; tests/, tests/fixtures/, docs/, seed/
