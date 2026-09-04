# nextERP mobile app

Mobile-first web app for the user's ERPNext/Frappe instance （销售订单 Sales
Order, 销售出货 Delivery Note, PDF printing). Runs as a systemd service
(`nexterp.service`, installed by `install-systemd.sh` — also handles
transitioning a manually-started instance off port 8347); for manual runs:
`bin/python server.py`. Open `http://<LAN-IP>:8347` directly, or via nginx at
`http://192.168.0.9/luciatrading/` (config: `nginx-luciatrading.conf`, installed
to `/etc/nginx/sites-available/luciatrading`). Frontend uses relative paths so
it works under the `/luciatrading/` prefix and standalone.

## Standing rules (user requirements)

- **NEVER use port 8000** for anything in this project. The app listens on
  port **8347** (randomly chosen, fixed in `server.py`; `PORT` env overrides).
  Any new service here must also avoid 8000. Port **8349** is taken by
  `llama-server.service` (local Qwen3-1.7B LLM, see below).
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
- Public access: `https://luciatrading.duckdns.org` → Caddy on the Frankfurt
  VPS (8.211.26.86) → `127.0.0.1:8347` → frp tcp tunnel (frps on Frankfurt,
  frpc on 192.168.0.103, shared token in their existing configs) →
  `192.168.0.9:8347`. The company website is served at
  `https://luciatrading.duckdns.org/www/` — Caddy rewrites `/www/*` to
  `/lucia/*` and proxies to `127.0.0.1:8097`, a second frp tcp proxy
  (`luciaweb`) → `192.168.0.9:80` (home nginx).
- **Planned (user deferred)**: the user owns `luciatrading.com` (Aliyun DNS)
  and wants to serve the company website on it directly from the Frankfurt
  nginx (not Caddy) when winery visits start. When that happens: add an
  A/AAAA record on Aliyun DNS → 8.211.26.86, serve the `www` branch `site/`
  from Frankfurt's nginx (or proxy it to the existing `luciaweb` tunnel on
  127.0.0.1:8097), set up TLS, and keep duckdns as fallback.
  Frankfurt runs other apps behind the same Caddy +
  frps; 192.168.0.103 runs other frpc proxies. **Always APPEND to
  `/etc/caddy/Caddyfile` and `/etc/frp/frpc.toml` — never overwrite**, keep
  the existing frp token, and use `caddy reload` / `frpc reload` (hot) so
  other apps are not interrupted.
- LAN HTTPS: mkcert CA (`~/.local/share/mkcert`), cert for 192.168.0.9 in
  `certs/` (gitignored, expires 2028-11), nginx serves 443 in the same
  homeserver block. CA is downloadable at `/lucia/mkcert-ca.pem` for phone
  install (Android: Settings → Security & privacy → Encryption & credentials
  → Install a certificate → CA; iOS: profile + Certificate Trust Settings).
  Needed because browsers only allow microphone over HTTPS.
- Credentials live in `.env` (`website`, `username`, `password`) and must
  stay server-side — never print them, never send them to the browser.
- Public auth: requests whose Host is `luciatrading.duckdns.org` require a
  password login (Flask session cookie, 31-day); LAN access stays open.
  Password hash + session secret in `app_config.json` (gitignored, never
  commit). To change the password: regenerate the hash with
  `werkzeug.security.generate_password_hash` and restart `server.py`.

## Architecture notes

- Local LLM (system-wide, not project-scoped): `llama-server.service`
  serves Qwen3-1.7B Q4_K_M via llama.cpp (Vulkan, MX350 GPU offload) as an
  OpenAI-compatible API on `127.0.0.1:8349` (LAN: `https://192.168.0.9/llm/v1`).
  Engine + model live in `/opt/llm/`; installer and docs live in the sibling
  project `../LLMqwen17/`. Any local app may use it. Practice runs showed
  ~3s/call with JSON-schema-constrained output; pinyin-annotated candidate
  lists were essential for accuracy.

- Branches: `main` = the mobile app; `www` = the company website (`site/`,
  EN/DE/FR/ES/IT). `site/` is gitignored on `main` — the live deployment is
  an untracked copy in this worktree (symlinked from `/srv/www/lucia`). To
  edit the website: `git checkout www`, edit `site/`, commit, push, then
  `git checkout main` and `git archive www site | tar -x` to refresh the
  live copy. Never `git checkout www` carelessly — switching branches
  removes/restores `site/`.

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
  submit ("提交出货（扣库存）") and print are separate buttons since 2026-08-15.
- Voice rules (user requirements):
  - Several ERP customers may share one spoken name (e.g. homophones
    叁年间/三年间). Resolve to the customer with the **newest ERP order**
    (`_customer_recent_order_rank` in `server.py`).
  - Items that share a spoken name across vintages: a **spoken vintage wins**
    （小海龙2025年 → the 2025 code, `_spoken_vintage`); when no vintage is
    spoken, resolve to the **newest vintage** (`_vintage` /
    `_latest_vintage_item`).
  - `voice_customer_eval.py` synthesizes spoken orders with edge-tts and tests
    the production ASR pipeline; `--harvest` folds Whisper mishearings back
    into `learned_aliases.json`. Never alias a phrase that is itself another
    customer's/item's spoken name. **Full testing/improvement workflow:
    `VOICE_TESTING.md`.**
- Careful with tests: never submit real Delivery Notes / Sales Orders except
  in a create → cancel → delete cleanup cycle.
