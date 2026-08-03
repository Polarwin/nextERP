# nextERP mobile app

Mobile-first web app for the user's ERPNext/Frappe instance （销售订单 Sales
Order, 销售出货 Delivery Note, PDF printing). Run: `bin/python server.py`,
open `http://<LAN-IP>:8347` directly, or via nginx at
`http://192.168.0.9/luciatrading/` (config: `nginx-luciatrading.conf`, installed
to `/etc/nginx/sites-available/luciatrading`). Frontend uses relative paths so
it works under the `/luciatrading/` prefix and standalone.

## Standing rules (user requirements)

- **NEVER use port 8000** for anything in this project. The app listens on
  port **8347** (randomly chosen, fixed in `server.py`; `PORT` env overrides).
  Any new service here must also avoid 8000.
- **Before touching nginx (or any shared system config), inspect what already
  exists.** This host runs several apps behind one nginx: one catch-all
  `default_server` block in `/etc/nginx/sites-available/homeserver` serves
  `/stockticker/` (proxy :8010), `/ytwatcher/` (static alias), `/valenciaguard`
  (redirect :8473), `/recipes/` (proxy :8293), and `/luciatrading/`.
  **Never create a new `server` block with `server_name 192.168.0.9`** — it
  hijacks Host matching and breaks all the other apps (this actually happened
  on 2026-08-03). Add new `location` blocks inside the existing `homeserver`
  server block instead. Read `/etc/nginx/sites-enabled/` and
  `/etc/nginx/sites-available/` first.
- Credentials live in `.env` (`website`, `username`, `password`) and must
  stay server-side — never print them, never send them to the browser.

## Architecture notes

- Setup: `bin/pip install -r requirements.txt && bin/python -m playwright install chromium`
- `server.py` — Flask backend. Proxies Frappe REST API, renders PDFs with
  Playwright Chromium (the ERP's own server-side PDF is broken: wkhtmltopdf
  fails on a broken image link in the print formats).
- `static/` — plain JS SPA (no build step), Chinese UI.
- `cache/*.json` — local cache of customers/items/warehouses (ERP is in
  China, user in Spain — high latency). Refreshed in background every 30 min;
  stale cache is served if refresh fails.
- Print formats: `Lucia2` (Sales Order), `销售出货` (Delivery Note).
- Stock is deducted by ERPNext on Delivery Note **submit** (not on print) —
  the app couples them in one "提交出货并打印" action.
- Careful with tests: never submit real Delivery Notes / Sales Orders except
  in a create → cancel → delete cleanup cycle.
