PICANTE DIRECT OPERATIONS — TEST BUILD

This is a separate local Flask/SQLite operations prototype. It is not connected to Base44, the existing WhatsApp bot, Render, WhatsApp, Meta, or any payment provider.

Included in this prototype:
- Dashboard and manual test-order creation
- Orders list and dispatcher assignment
- Approved/active driver records
- Guard against assigning an order to an unapproved/inactive driver
- Guard intended to prevent a third unsettled delivery before settlement
- Cash settlement ledger and driver balances
- Separate MX$150 backpack deposit record
- Company-card transaction ledger linked to driver and order
- Day/overnight prices in test order creation: MX$225 per pizza 06:00–23:59; MX$275 00:00–05:59

IMPORTANT LIMITATIONS:
- This is a functional prototype, not production-ready software.
- No authentication/roles, audit-log protection, concurrency hardening, WhatsApp group broadcast, driver self-claim UI, GPS, or live WhatsApp connection.
- Current settlement/cash accounting is simplified. Before operational use, confirm whether the amount handed to office should be the machine pizza value (MX$175 per pizza) and whether overnight pricing's MX$100 add-on is all driver compensation; the current formula is provisional.
- Do not expose this app publicly or use for real customer financial records until security, data backup, concurrency, and accounting rules are reviewed.
- Run locally:
  1. python3 -m venv .venv
  2. source .venv/bin/activate
  3. pip install -r requirements.txt
  4. python app.py
  5. open http://127.0.0.1:5000
