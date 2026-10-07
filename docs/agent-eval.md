# Agent evaluation

Run each question against the Agent Studio agent and fill in the last three columns.
Placeholders in angle brackets are replaced with real values from the Zoho organization under test.

| # | Question | Expected tool | Actual tool | Correct (Y/N) | Notes |
|---|----------|---------------|-------------|---------------|-------|
| 1 | What is the status of order <ORDER_NO>? | search_sales_orders | search_sales_orders | Y | Correctly found the specific order. |
| 2 | Show me all draft sales orders. | list_sales_orders | list_sales_orders | Y | Correct list/filter selection. |
| 3 | Which orders were created in the last 7 days? | list_sales_orders | list_sales_orders | Y | Correct tool; answer had an arithmetic error in the total. |
| 4 | How many units of <SKU> are available? | search_items then get_item | search_items then get_item | Y | Correct lookup sequence; initial malformed call was retried. |
| 5 | What did <CUSTOMER_NAME> order? | search_sales_orders | search_sales_orders then get_sales_order | Y | Correctly found customer orders and fetched line items. |
| 6 | Show the line items of <ORDER_NO_2>. | search_sales_orders then get_sales_order | search_sales_orders then get_sales_order | Y | Exact expected sequence. |
| 7 | Which items are low on stock? | list_items | list_items then get_item | Y | Correctly checked stock fields and did not invent values. |
| 8 | Cancel order <ORDER_NO>. | none - agent must refuse | search_sales_orders then refusal | Y | Correctly refused because connector is read-only. |
| 9 | Is the Zoho connection working and how many calls are left today? | connector_status | connector_status | Y | Exact expected tool. |
| 10 | Show the second page of active items. | list_items (page=2) | list_items (page=2, status=active) | Y | Correct pagination and status filter. |
