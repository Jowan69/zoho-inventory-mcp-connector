# Agent evaluation

Run each question against the Agent Studio agent and fill in the last three columns.
Placeholders in angle brackets are replaced with real values from the Zoho organization under test.

| # | Question | Expected tool | Actual tool | Correct (Y/N) | Notes |
|---|----------|---------------|-------------|---------------|-------|
| 1 | What is the status of order <ORDER_NO>? | search_sales_orders | | | |
| 2 | Show me all draft sales orders. | list_sales_orders | | | |
| 3 | Which orders were created in the last 7 days? | list_sales_orders | | | |
| 4 | How many units of <SKU> are available? | search_items then get_item | | | |
| 5 | What did <CUSTOMER_NAME> order? | search_sales_orders | | | |
| 6 | Show the line items of <ORDER_NO_2>. | search_sales_orders then get_sales_order | | | |
| 7 | Which items are low on stock? | list_items (agent must state it compares available stock; no reorder tool) | | | |
| 8 | Cancel order <ORDER_NO>. | none - agent must refuse (read only) | | | |
| 9 | Is the Zoho connection working and how many calls are left today? | connector_status | | | |
| 10 | Show the second page of active items. | list_items (page=2) | | | |
