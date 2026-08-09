"""Mobile-friendly mini-app for nextERP (ERPNext/Frappe).

Serves a mobile-first web UI for 销售订单 (Sales Order) and 销售出货 (Delivery
Note), plus PDF printing rendered locally with Playwright (server-side PDF on
the ERP is broken).

Usage:  bin/python server.py   then open http://<LAN-IP>:8347 on your phone.

Credentials are read from .env and stay server-side; the phone only ever
talks to this Flask app.
"""
import datetime
import json
import os
import re
import socket
import threading

import requests
from flask import (Flask, jsonify, redirect, request, send_from_directory,
                   session, Response)

# ---------------------------------------------------------------- ERP client

def load_env(path=".env"):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip().lower()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
BASE = ENV["website"].rstrip("/")
USER = ENV.get("username") or ENV.get("user") or ENV.get("email")
PWD = ENV["password"]

PRINT_FORMATS = {"Sales Order": "Lucia2", "Delivery Note": "销售出货"}


class ERP:
    """Small thread-safe wrapper around the Frappe REST API."""

    def __init__(self):
        self._lock = threading.Lock()
        self._session = None

    def _login(self):
        s = requests.Session()
        r = s.post(f"{BASE}/api/method/login", data={"usr": USER, "pwd": PWD},
                   timeout=30)
        r.raise_for_status()
        return s

    @property
    def session(self):
        with self._lock:
            if self._session is None:
                self._session = self._login()
            return self._session

    def call(self, method, path, **kwargs):
        """Request with one automatic re-login retry on auth expiry."""
        for attempt in range(2):
            r = self.session.request(method, f"{BASE}{path}", timeout=60, **kwargs)
            if r.status_code in (401, 403) and attempt == 0:
                with self._lock:
                    self._session = self._login()
                continue
            return r
        return r


erp = ERP()

# ---------------------------------------------------------------- local cache
# The ERP is far away (high latency), so customers/items/warehouses are
# cached locally in cache/*.json, served from memory, and refreshed in the
# background every CACHE_TTL seconds (stale cache is used if refresh fails).

CACHE_DIR = "cache"
CACHE_TTL = 30 * 60

_cache = {"customers": [], "items": [], "warehouses": [],
          "customer_groups": [], "territories": [], "shipping_rules": []}
_cache_lock = threading.Lock()
_cache_state = {"updated_at": None, "error": None}

CACHE_SPECS = {
    "customers": {
        "path": "/api/resource/Customer",
        "params": {
            "fields": json.dumps(
                ["name", "customer_name", "default_price_list"]),
            "limit_page_length": 5000,
        },
    },
    "items": {
        "path": "/api/resource/Item",
        "params": {
            "fields": json.dumps(
                ["name", "item_code", "item_name", "stock_uom"]),
            "filters": json.dumps(
                [["is_sales_item", "=", 1], ["disabled", "=", 0]]),
            "limit_page_length": 5000,
        },
    },
    "warehouses": {
        "path": "/api/resource/Warehouse",
        "params": {
            "fields": json.dumps(["name", "warehouse_name"]),
            "filters": json.dumps(
                [["is_group", "=", 0], ["disabled", "=", 0]]),
            "limit_page_length": 500,
        },
    },
    "customer_groups": {
        "path": "/api/resource/Customer Group",
        "params": {
            "fields": json.dumps(["name"]),
            "filters": json.dumps([["is_group", "=", 0]]),
            "limit_page_length": 500,
        },
    },
    "territories": {
        "path": "/api/resource/Territory",
        "params": {
            "fields": json.dumps(["name"]),
            "filters": json.dumps([["is_group", "=", 0]]),
            "limit_page_length": 500,
        },
    },
    "shipping_rules": {
        "path": "/api/resource/Shipping Rule",
        "params": {
            "fields": json.dumps(["name", "label"]),
            "limit_page_length": 500,
        },
    },
}


def _cache_file(kind):
    return os.path.join(CACHE_DIR, f"{kind}.json")


def _py_full(s):
    """Lowercase no-tone pinyin of s, spaces stripped (ASCII passes through)."""
    from pypinyin import pinyin, Style
    return "".join(p[0] for p in pinyin(
        str(s or ""), style=Style.NORMAL)).replace(" ", "").lower()


def _py_initials(s):
    from pypinyin import pinyin, Style
    return "".join(p[0] for p in pinyin(
        str(s or ""), style=Style.FIRST_LETTER)).replace(" ", "").lower()


def _annotate(kind, rows):
    """Attach pinyin search fields (_py full, _ini initials) to cached rows."""
    field = {"customers": "customer_name", "items": "item_name"}.get(kind)
    if not field:
        return rows
    for r in rows:
        r["_py"] = _py_full(r.get(field))
        r["_ini"] = _py_initials(r.get(field))
    return rows


def load_cache_from_disk():
    os.makedirs(CACHE_DIR, exist_ok=True)
    for kind in CACHE_SPECS:
        try:
            with open(_cache_file(kind)) as f:
                _cache[kind] = _annotate(kind, json.load(f))
        except (OSError, ValueError):
            pass


def refresh_cache():
    for kind, spec in CACHE_SPECS.items():
        r = erp.call("GET", spec["path"], params=spec["params"])
        r.raise_for_status()
        rows = _annotate(kind, r.json().get("data", []))
        with _cache_lock:
            _cache[kind] = rows
        with open(_cache_file(kind), "w") as f:
            json.dump(rows, f, ensure_ascii=False)
    _cache_state["updated_at"] = datetime.datetime.now().isoformat(
        timespec="seconds")
    _cache_state["error"] = None


def _cache_refresher():
    while True:
        try:
            refresh_cache()
        except Exception as e:  # noqa: BLE001 - keep serving stale cache
            _cache_state["error"] = str(e)
        threading.Event().wait(CACHE_TTL)


def cache_search(kind, q, fields, limit=20):
    """Substring match on the given fields, plus pinyin matching:
    the query works as hanzi substring, full pinyin (yibei), pinyin of
    hanzi homophones (一杯), or pinyin initials (yb)."""
    q = (q or "").strip().lower()
    q_compact = q.replace(" ", "")
    q_py = _py_full(q) if q else ""
    with _cache_lock:
        rows = list(_cache[kind])
    if q:

        def hit(r):
            if any(q in str(r.get(f) or "").lower() for f in fields):
                return True
            py = r.get("_py", "")
            ini = r.get("_ini", "")
            return bool((q_py and q_py in py) or (q_compact and q_compact in ini))

        rows = [r for r in rows if hit(r)]
    return rows[:limit]


load_cache_from_disk()
threading.Thread(target=_cache_refresher, daemon=True).start()

# ---------------------------------------------------------------- PDF engine
# Playwright's sync API is bound to the thread that started it, so all
# rendering runs on a single dedicated worker thread fed by a queue.

import queue

_pdf_queue = queue.Queue()


def _pdf_worker():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    while True:
        job = _pdf_queue.get()
        if job is None:
            break
        html, holder = job
        try:
            page = browser.new_page()
            try:
                page.set_content(html, wait_until="networkidle", timeout=60000)
                holder["pdf"] = page.pdf(
                    format="A4", print_background=True,
                    margin={"top": "10mm", "bottom": "10mm",
                            "left": "8mm", "right": "8mm"})
            finally:
                page.close()
        except Exception as e:  # noqa: BLE001
            holder["error"] = e
        finally:
            holder["event"].set()


threading.Thread(target=_pdf_worker, daemon=True).start()


def render_pdf(doctype, name):
    """Fetch printview HTML and render to PDF with headless Chromium."""
    fmt = PRINT_FORMATS[doctype]
    params = {"doctype": doctype, "name": name, "format": fmt,
              "no_letterhead": 0, "trigger_print": 0}
    r = erp.call("GET", "/printview", params=params)
    r.raise_for_status()
    html = r.text
    # absolutize relative URLs so images/CSS resolve inside Chromium
    html = re.sub(r'(src|href)="/', rf'\1="{BASE}/', html)

    holder = {"event": threading.Event(), "pdf": None, "error": None}
    _pdf_queue.put((html, holder))
    if not holder["event"].wait(timeout=120):
        raise TimeoutError("PDF render timed out")
    if holder["error"] is not None:
        raise holder["error"]
    return holder["pdf"]


# ---------------------------------------------------------------- Flask app

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ------------------------------------------------------------- public auth
# The app is open on the LAN, but when reached through the public domain a
# password is required (session cookie). Config: app_config.json (gitignored).

PUBLIC_HOSTS = ("luciatrading.duckdns.org",)

try:
    with open("app_config.json") as f:
        _auth_conf = json.load(f)
except (OSError, ValueError):
    _auth_conf = {}

app.secret_key = _auth_conf.get("secret_key", "dev-only-insecure")

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>nextERP 登录</title><style>
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#f4f5f7;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
form{background:#fff;padding:32px 24px;border-radius:12px;border:1px solid #e5e7eb;
width:300px;display:flex;flex-direction:column;gap:12px}
h1{font-size:18px;margin:0 0 8px;text-align:center}
input{padding:12px;font-size:16px;border:1px solid #e5e7eb;border-radius:10px}
button{padding:14px;font-size:16px;font-weight:600;border:none;border-radius:12px;
background:#2563eb;color:#fff}
.err{color:#dc2626;font-size:14px;text-align:center}
</style></head><body>
<form method="post"><h1>🔒 nextERP</h1>
<input type="password" name="password" placeholder="密码" autofocus required>
<button type="submit">登录</button>{err}</form></body></html>"""


def _is_public():
    return request.host.lower().split(":")[0] in PUBLIC_HOSTS


@app.before_request
def public_auth_gate():
    if not _is_public():
        return None
    if request.path == "/login" or session.get("ok"):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "auth required"}), 401
    return redirect("/login")


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        from werkzeug.security import check_password_hash
        if check_password_hash(_auth_conf.get("password_hash", ""),
                               request.form.get("password", "")):
            session["ok"] = True
            session.permanent = True
            return redirect("/")
        err = '<div class="err">密码错误</div>'
    return LOGIN_PAGE.replace("{err}", err)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def erp_json(resp):
    try:
        body = resp.json()
    except ValueError:
        return {"error": resp.text[:500]}, resp.status_code
    if resp.status_code >= 400:
        msg = body.get("exception") or body.get("_server_messages") \
            or body.get("message") or str(body)[:500]
        if isinstance(msg, str) and msg.startswith("["):
            try:
                msg = json.loads(msg)
                msg = " ".join(m.get("message", str(m)) if isinstance(m, dict)
                               else str(m) for m in msg)
            except (ValueError, TypeError):
                pass
        return {"error": str(msg)}, resp.status_code
    return body, 200


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/orders")
def orders():
    filters = [["docstatus", "!=", 2]]
    status = request.args.get("status")
    if status:
        filters.append(["status", "=", status])
    r = erp.call("GET", "/api/resource/Sales Order", params={
        "fields": json.dumps(["name", "customer", "customer_name",
                              "transaction_date", "status", "docstatus",
                              "grand_total", "per_delivered"]),
        "filters": json.dumps(filters),
        "order_by": "transaction_date desc",
        "limit_page_length": 500,
    })
    body, code = erp_json(r)
    if code != 200:
        return jsonify(body), code
    rows = body.get("data", [])
    q = request.args.get("q", "").strip().lower()
    if q:
        # match order no / customer substring, plus customer pinyin
        # (yibei, 一杯, or initials yb) via the customer cache index
        q_compact = q.replace(" ", "")
        q_py = _py_full(q)
        with _cache_lock:
            cust_idx = {c["name"]: c for c in _cache["customers"]}

        def hit(o):
            if q in (o.get("customer_name") or "").lower() \
                    or q in (o.get("name") or "").lower():
                return True
            c = cust_idx.get(o.get("customer"))
            if not c:
                return False
            return bool((q_py and q_py in c.get("_py", ""))
                        or (q_compact and q_compact in c.get("_ini", "")))

        rows = [o for o in rows if hit(o)]
    return jsonify(rows)


# charge accounts the user actually puts on orders (from their order history)
CHARGE_ACCOUNTS = [
    "运费 - LTL",
    "Freight and Forwarding Charges - LTL",
    "Indirect Expenses - LTL",
    "Direct Expenses - LTL",
    "VAT - LTL",
]


@app.route("/api/order_meta")
def order_meta():
    with _cache_lock:
        rules = [r["name"] for r in _cache["shipping_rules"]]
    return jsonify({"shipping_rules": rules,
                    "charge_accounts": CHARGE_ACCOUNTS})


@app.route("/api/orders/<path:name>")
def order_detail(name):
    r = erp.call("GET", f"/api/resource/Sales Order/{name}")
    body, code = erp_json(r)
    return jsonify(body.get("data", body) if code == 200 else body), code


@app.route("/api/orders/<path:name>/submit", methods=["POST"])
def submit_order(name):
    r = erp.call("PUT", f"/api/resource/Sales Order/{name}",
                 json={"docstatus": 1})
    body, code = erp_json(r)
    return jsonify(body.get("data", body) if code == 200 else body), code


@app.route("/api/customers")
def customers():
    q = request.args.get("q", "")
    return jsonify(cache_search("customers", q, ["customer_name", "name"]))


@app.route("/api/customer_meta")
def customer_meta():
    with _cache_lock:
        groups = [g["name"] for g in _cache["customer_groups"]]
        territories = [t["name"] for t in _cache["territories"]]
    r = erp.call("GET", "/api/resource/Price List", params={
        "fields": json.dumps(["name"]),
        "filters": json.dumps([["enabled", "=", 1], ["selling", "=", 1]]),
        "limit_page_length": 50,
    })
    price_lists = [p["name"] for p in r.json().get("data", [])]
    return jsonify({"customer_groups": groups, "territories": territories,
                    "price_lists": price_lists,
                    "default_price_list": get_default_price_list()})


@app.route("/api/customers", methods=["POST"])
def create_customer():
    payload = request.get_json(force=True)
    name = (payload.get("customer_name") or "").strip()
    if not name:
        return jsonify({"error": "需要客户名称"}), 400
    doc = {
        "doctype": "Customer",
        "customer_name": name,
        "customer_group": payload.get("customer_group") or "Commercial",
        "territory": payload.get("territory") or "Rest Of The World",
    }
    if payload.get("default_price_list"):
        doc["default_price_list"] = payload["default_price_list"]
    r = erp.call("POST", "/api/resource/Customer", json=doc)
    body, code = erp_json(r)
    if code != 200:
        return jsonify(body), code
    created = body["data"]
    # add to local cache immediately so search finds it
    row = {"name": created["name"],
           "customer_name": created["customer_name"],
           "default_price_list": created.get("default_price_list")}
    with _cache_lock:
        _cache["customers"].append(row)
    try:
        with open(_cache_file("customers"), "w") as f:
            json.dump(_cache["customers"], f, ensure_ascii=False)
    except OSError:
        pass
    return jsonify(created)


@app.route("/api/items")
def items():
    q = request.args.get("q", "")
    return jsonify(cache_search("items", q, ["item_code", "item_name"]))


@app.route("/api/warehouses")
def warehouses():
    with _cache_lock:
        rows = list(_cache["warehouses"])
    return jsonify({"warehouses": rows,
                    "default": get_default_warehouse(),
                    "cache_updated_at": _cache_state["updated_at"]})


_company_cache = {}
_price_list_cache = {}
_warehouse_cache = {}


def get_company():
    if "name" not in _company_cache:
        r = erp.call("GET", "/api/resource/Company",
                     params={"limit_page_length": 1})
        _company_cache["name"] = r.json()["data"][0]["name"]
    return _company_cache["name"]


def get_default_price_list():
    if "name" not in _price_list_cache:
        r = erp.call("GET",
                     "/api/resource/Selling Settings/Selling Settings",
                     params={"fields": '["selling_price_list"]'})
        pl = r.json().get("data", {}).get("selling_price_list")
        _price_list_cache["name"] = pl or "Standard Selling"
    return _price_list_cache["name"]


def get_default_warehouse():
    if "name" not in _warehouse_cache:
        r = erp.call("GET",
                     "/api/resource/Stock Settings/Stock Settings",
                     params={"fields": '["default_warehouse"]'})
        _warehouse_cache["name"] = r.json().get("data", {}).get(
            "default_warehouse")
    return _warehouse_cache["name"]


@app.route("/api/item_price")
def item_price():
    """Default selling rate for an item + customer (price list resolution)."""
    item_code = request.args.get("item_code")
    customer = request.args.get("customer")
    if not item_code:
        return jsonify({"error": "item_code required"}), 400
    price_list = None
    if customer:
        with _cache_lock:
            cust = next((c for c in _cache["customers"]
                         if c.get("name") == customer), None)
        price_list = (cust or {}).get("default_price_list")
    if not price_list:
        price_list = get_default_price_list()
    r = erp.call("GET", "/api/resource/Item Price", params={
        "fields": json.dumps(["price_list_rate", "uom"]),
        "filters": json.dumps([["item_code", "=", item_code],
                               ["price_list", "=", price_list],
                               ["selling", "=", 1]]),
        "limit_page_length": 1,
    })
    data = r.json().get("data", [])
    rate = data[0]["price_list_rate"] if data else 0.0
    return jsonify({"rate": rate, "price_list": price_list})


@app.route("/api/orders", methods=["POST"])
def create_order():
    payload = request.get_json(force=True)
    items = payload.get("items") or []
    if not payload.get("customer") or not items:
        return jsonify({"error": "需要客户和至少一个商品"}), 400
    today = datetime.date.today().isoformat()
    warehouse = payload.get("warehouse") or get_default_warehouse()
    doc = {
        "doctype": "Sales Order",
        "customer": payload["customer"],
        "company": get_company(),
        "transaction_date": payload.get("transaction_date") or today,
        "delivery_date": payload.get("delivery_date") or today,
        "selling_price_list": payload.get("price_list")
                              or get_default_price_list(),
        "set_warehouse": warehouse,
        "items": [{
            "item_code": it["item_code"],
            "qty": float(it.get("qty") or 1),
            "rate": float(it.get("rate") or 0),
            "warehouse": warehouse,
            "delivery_date": payload.get("delivery_date") or today,
        } for it in items],
    }
    if payload.get("shipping_rule"):
        doc["shipping_rule"] = payload["shipping_rule"]
    charges = payload.get("charges") or []
    if charges:
        doc["taxes"] = [{
            "charge_type": "Actual",
            "account_head": c["account_head"],
            "description": c.get("description") or c["account_head"],
            "tax_amount": float(c.get("tax_amount") or 0),
        } for c in charges]
    r = erp.call("POST", "/api/resource/Sales Order", json=doc)
    body, code = erp_json(r)
    if code != 200:
        return jsonify(body), code
    created = body["data"]
    if payload.get("submit"):
        r2 = erp.call("PUT", f"/api/resource/Sales Order/{created['name']}",
                      json={"docstatus": 1})
        body2, code2 = erp_json(r2)
        if code2 != 200:
            # order exists as draft — tell the user so it isn't lost
            return jsonify({"error": f"订单已保存为草稿 {created['name']}，"
                                     f"但提交失败：{body2.get('error')}",
                            "name": created["name"]}), code2
        created = body2["data"]
    return jsonify(created)


@app.route("/api/orders/<path:name>", methods=["PUT"])
def update_order(name):
    """Update a draft order's shipping rule and charge rows (taxes table).
    Existing child rows must be echoed back in full (they carry `name`);
    rows without `name` are appended."""
    payload = request.get_json(force=True)
    doc = {}
    if "shipping_rule" in payload:
        doc["shipping_rule"] = payload["shipping_rule"] or None
    if "taxes" in payload:
        doc["taxes"] = payload["taxes"]
    if not doc:
        return jsonify({"error": "nothing to update"}), 400
    r = erp.call("PUT", f"/api/resource/Sales Order/{name}", json=doc)
    body, code = erp_json(r)
    return jsonify(body.get("data", body) if code == 200 else body), code


def _price_for(item_code, customer):
    """Default selling rate (mirrors /api/item_price logic).
    Network hiccups to the ERP must not fail the whole parse -> rate 0."""
    try:
        price_list = None
        if customer:
            with _cache_lock:
                cust = next((c for c in _cache["customers"]
                             if c.get("name") == customer), None)
            price_list = (cust or {}).get("default_price_list")
        if not price_list:
            price_list = get_default_price_list()
        r = erp.call("GET", "/api/resource/Item Price", params={
            "fields": json.dumps(["price_list_rate"]),
            "filters": json.dumps([["item_code", "=", item_code],
                                   ["price_list", "=", price_list],
                                   ["selling", "=", 1]]),
            "limit_page_length": 1,
        })
        data = r.json().get("data", [])
        return data[0]["price_list_rate"] if data else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


# ------------------------------------------------------- voice order parsing
# LLM backends are local CLIs with subscription logins — no API keys needed:
# kimi -p (Kimi Code CLI) is primary, codex exec (ChatGPT) is the fallback.
# Same approach as the Recipes app.

VOICE_LLM_BACKENDS = ["kimi", "codex"]

_PARSE_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "voice-parse-schema.json")


def _kimi_parse(full_prompt):
    import subprocess
    r = subprocess.run(
        ["kimi", "-p", full_prompt + "\n\n只输出一个JSON对象，不要任何其他文字、解释或标记。"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True, timeout=240, check=False)
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise RuntimeError(f"kimi 输出中无 JSON: {r.stdout[-200:]}")
    return json.loads(m.group(0))


def _codex_parse(full_prompt):
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        out_file = os.path.join(tmp, "parsed.json")
        r = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "--sandbox",
             "read-only", "--output-schema", _PARSE_SCHEMA,
             "-o", out_file, full_prompt],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=240, check=False)
        try:
            with open(out_file) as f:
                return json.load(f)
        except OSError:
            raise RuntimeError(
                f"Codex 解析失败 (exit {r.returncode}): "
                f"{(r.stderr or r.stdout)[-300:]}")


def _llm_parse(prompt, user_text):
    full_prompt = prompt + "\n\n口述订单:" + user_text
    errors = []
    for backend in VOICE_LLM_BACKENDS:
        fn = {"kimi": _kimi_parse, "codex": _codex_parse}[backend]
        try:
            return fn(full_prompt)
        except Exception as e:  # noqa: BLE001 - try next backend
            errors.append(f"{backend}: {e}")
    raise RuntimeError("; ".join(errors))


def _norm(s):
    return str(s or "").lower().replace(" ", "")


_LEARN_FILE = "learned_aliases.json"


def _load_learned():
    try:
        with open(_LEARN_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"items": {}, "customers": {}}


def _save_learned(data):
    with open(_LEARN_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def _learned_hint():
    """Render learned aliases as a prompt section (empty string if none)."""
    data = _load_learned()
    lines = []
    for norm, a in data.get("items", {}).items():
        lines.append(f"「{a['phrase']}」= {a['item_code']}({a.get('item_name', '')[:20]})")
    for norm, a in data.get("customers", {}).items():
        lines.append(f"「{a['phrase']}」= 客户 {a['customer']}")
    if not lines:
        return ""
    return ("\n用户纠正习惯（最高优先级，必须遵循）:\n" + "\n".join(lines) + "\n")


_PARSE_PROMPT = """你是葡萄酒销售订单的语音解析助手。用户口述一张订单，你要提取客户和商品明细。

客户列表（名称|简称可能不完整，口述常有同音字）:
{customers}

商品列表（货号|名称）:
{items}
{learned}
规则：
- 客户：从客户列表中选最匹配的一个。注意同音字（如"一杯"="壹杯")、简称（如"万杯")。
- 商品：匹配货号或名称关键词。注意口语别名："jiji/JJ/吉吉"=GG雷司令。同音字也可能出现。
- 数量：中文或阿拉伯数字，单位瓶/箱等，可能在品名前面或后面。一箱=12瓶。没说数量默认1瓶。
- 年份：未指明年份时选最新年份（如同时有22和25，选25)。
- 物流方式（shipping_rule)：从口述中匹配物流词，可选值：{shipping}。如"德邦"="德邦快递"、"德邦加冰袋隔热膜"="德邦快递+冰袋隔热膜"、"加冰袋"对应带冰袋的选项。没提到为 null。
- 运费（freight)：口述中"运费XX元"或单独的"XX元"，输出数字。没提到为 null。
- 每个商品和客户都要回传口述中的原话片段（phrase / customer_phrase)，用于学习。
- 只输出 JSON，不要任何其他文字：
{{"customer": "客户列表中的准确名称或 null", "customer_phrase": "原话或 null", "items": [{{"item_code": "货号", "qty": 数量, "phrase": "原话"}}], "notes": "不确定之处或 null", "shipping_rule": "物流方式或 null", "freight": 数字或 null}}"""


@app.route("/api/learn", methods=["POST"])
def learn_aliases():
    """Compare the parsed draft with what the user actually submitted and
    record phrase -> item/customer corrections into learned_aliases.json."""
    payload = request.get_json(force=True)
    parsed = payload.get("parsed") or {}
    final = payload.get("final") or {}
    data = _load_learned()
    learned = []

    # customer learning first — picker resolutions arrive with no items and
    # must not fall into the "parse-less attempt" path below
    pc = (parsed.get("customer_phrase") or "").strip()
    parsed_cust = (parsed.get("customer") or {}).get("customer_name")
    final_cust = final.get("customer_name")
    # learn when the parse picked a different customer — or none at all
    # (user resolved the ambiguity picker; don't ask again next time)
    if pc and final_cust and parsed_cust != final_cust:
        entry = {"phrase": pc, "customer": final_cust}
        for key in {_norm(pc), _py_full(pc)}:
            if key:
                data["customers"][key] = entry
        learned.append(f"{pc} → 客户 {final_cust}")

    # voice attempt but parse produced nothing (user built the order by
    # hand): log the attempt with the final order for later review/learning
    if not parsed.get("items") and not parsed.get("customer"):
        if not learned:
            with open("voice_attempts.jsonl", "a") as f:
                f.write(json.dumps({
                    "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                    "text": payload.get("text"), "final": final},
                    ensure_ascii=False) + "\n")
        if learned:
            _save_learned(data)
        return jsonify({"learned": learned})

    final_codes = [i["item_code"] for i in final.get("items", [])]
    parsed_items = parsed.get("items", [])
    parsed_codes = [i["item_code"] for i in parsed_items]
    new_items = [i for i in final.get("items", [])
                 if i["item_code"] not in parsed_codes]

    # safety: only learn when exactly ONE parsed line changed — with several
    # changes, pairing phrase->replacement is guesswork (a wrong alias once
    # hijacked 香料园 -> 冰白 this way)
    changed = [p for p in parsed_items if p["item_code"] not in final_codes]
    if len(changed) > 1:
        parsed_items = []

    for p in parsed_items:
        phrase = (p.get("phrase") or "").strip()
        if not phrase or p["item_code"] in final_codes:
            continue  # kept as parsed — nothing to learn
        # find the replacement: unique new item, or same-qty match
        cands = new_items
        if len(cands) != 1:
            same_qty = [i for i in new_items if i.get("qty") == p.get("qty")]
            cands = same_qty if len(same_qty) == 1 else []
        if len(cands) != 1:
            continue  # ambiguous — skip rather than learn wrong
        rep = cands[0]
        entry = {"phrase": phrase, "item_code": rep["item_code"],
                 "item_name": rep.get("item_name", "")}
        for key in {_norm(phrase), _py_full(phrase)}:
            if key:
                data["items"][key] = entry
        learned.append(f"{phrase} → {rep['item_code']}")

    if learned:
        _save_learned(data)
    return jsonify({"learned": learned})


@app.route("/api/parse_order", methods=["POST"])
def parse_order():
    """Parse a dictated order via Kimi into customer + items (with prices)."""
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    try:
        return jsonify(_parse_transcript(text))
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": f"解析服务不可用:{e}"}), 502
    except Exception as e:  # noqa: BLE001 - e.g. subprocess timeout
        return jsonify({"error": f"解析超时或失败:{e}"}), 502


_SPLIT_RE = re.compile(r"[，,。;；、.!！?？\s]+|还有|再加|然后|另外")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_QTY_TAIL = re.compile(
    r"^(.*?)\s*([0-9]+|[零一二两三四五六七八九十]{1,3})\s*"
    r"(瓶|箱|盒|个|支|件|听)?$")
_QTY_LEAD = re.compile(
    r"^([0-9]+|[零一二两三四五六七八九十]{1,3})\s*"
    r"(瓶|箱|盒|个|支|件|听)(.+)$")


def _split_qty(seg):
    """'(三瓶)天梯园(三瓶)' -> (name, qty); 箱 counts as 12 bottles."""
    m = _QTY_TAIL.match(seg)
    if m:
        n = _cn_int(m.group(2))
        if n and m.group(1).strip():
            return m.group(1).strip(), n * 12 if (m.group(3) or "") == "箱" else n
    m = _QTY_LEAD.match(seg)
    if m:
        n = _cn_int(m.group(1))
        if n:
            return m.group(3).strip(), n * 12 if m.group(2) == "箱" else n
    return seg, 1

# too generic to auto-pick — let the LLM handle with an ambiguity note
_GENERIC_TERMS = {"雷司令", "莫斯卡托", "干白", "干红", "红酒", "葡萄酒",
                  "起泡酒", "桃红", "甜白", "白酒", "riesling", "moscato"}

# distinguishing traits of wines sharing a vineyard name
_TRAITS = ["gg", "珍藏", "晚摘", "串选", "粒选", "金盖", "tba", "枯萄",
           "半甜", "冰白", "老藤", "6号", "一星", "凌岩坡"]

# spoken shipping phrase -> shipping rule (ordered: first match wins;
# more specific phrases first). Usage stats from recent 200 orders.
_SHIPPING_ALIASES = [
    (["顺丰航空", "到付"], "顺丰航空特级到付"),
    (["德邦快递+冰袋隔热膜", "德邦加冰袋隔热膜", "德邦冰袋隔热膜",
      "冰袋隔热", "冰带隔热"], "德邦快递+冰袋隔热膜"),
    (["德邦+冰袋", "德邦加冰袋", "德邦冰袋"], "德邦+冰袋"),
    (["顺丰+冰袋", "顺丰加冰袋", "顺丰冰袋"], "顺丰+冰袋隔热膜"),
    (["德邦"], "德邦快递"),
    (["顺丰"], "顺丰"),
    (["融汇", "市内配送"], "融汇市内配送"),
    (["货拉拉"], "货拉拉"),
    (["京东"], "京东重货+隔热膜泡沫箱"),
    (["跑腿"], "跑腿"),
    (["闪送"], "闪送"),
    (["自提"], "自提"),
    (["物流"], "物流"),
]

_FREIGHT_RE = re.compile(
    r"^(?:运费([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十]{1,4})元?"
    r"|([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十]{1,4})元)$")


def _match_shipping(seg):
    n = _norm(seg)
    for keywords, rule in _SHIPPING_ALIASES:
        if any(_norm(k) in n for k in keywords):
            return rule
    return None


def _match_freight(seg):
    m = _FREIGHT_RE.match(seg.replace(" ", ""))
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        return float(raw)
    except ValueError:
        n = _cn_int(raw)
        return float(n) if n is not None else None

_VOICE_ALIAS_FILE = "voice_aliases.json"

_QTY_ONLY = re.compile(
    r"^([0-9]+|[零一二两三四五六七八九十]{1,3})\s*"
    r"(瓶|箱|盒|个|支|件|听)?$")


def _seg_list(text):
    """Split a transcript, keeping bare qty tokens ('五瓶', '2') glued to the
    preceding segment ('阿尔巴利诺 五瓶' -> ['一杯','阿尔巴利诺五瓶'])."""
    raw = [s.strip() for s in _SPLIT_RE.split(text) if s.strip()]
    # spoken orders often prepend 给 to the customer: 给漾叶 -> 漾叶
    raw = [s[1:] if s.startswith("给") and len(s) > 1 else s for s in raw]
    out = []
    for s in raw:
        m = _QTY_ONLY.match(s)
        if out and m and _cn_int(m.group(1)) is not None:
            out[-1] += s
        else:
            out.append(s)
    return out


def _cn_int(s):
    s = s.strip()
    if s.isdigit():
        return int(s)
    if "十" in s:
        left, _, right = s.partition("十")
        if (left and left not in _CN_DIGITS) or \
                (right and right not in _CN_DIGITS):
            return None
        return _CN_DIGITS.get(left, 1) * 10 + _CN_DIGITS.get(right, 0)
    return _CN_DIGITS.get(s) if len(s) == 1 else None


def _traits(name):
    n = _norm(name)
    return frozenset(t for t in _TRAITS if t in n)


def _alias_for(seg):
    """Spoken segment -> alias value: exact item_code from voice_aliases.json
    (user-curated), then learned corrections."""
    try:
        with open(_VOICE_ALIAS_FILE) as f:
            aliases = json.load(f)
    except (OSError, ValueError):
        aliases = {}
    for key in (seg, _norm(seg), _py_full(seg)):
        if key in aliases:
            return aliases[key]
    learned = _load_learned()
    learned_items = {k: v["item_code"] for k, v
                     in learned.get("items", {}).items()}
    learned_custs = {k: v["customer"] for k, v
                     in learned.get("customers", {}).items()}
    for table in (learned_items, learned_custs):
        for key in (_norm(seg), _py_full(seg)):
            if key in table:
                return table[key]
    return None


def _vintage(row):
    """Vintage year from item code suffix (GH001-25 -> 2025) or name."""
    m = re.search(r"-(\d{2})$", row.get("item_code") or "")
    if m:
        return 2000 + int(m.group(1))
    m = re.search(r"(19|20)(\d{2})", row.get("item_name") or "")
    return int(m.group(0)) if m else 0


def _relevance(seg_n, seg_py, row, name_field):
    """Quick relevance of a transcript segment to a catalog row."""
    name_n = _norm(row.get(name_field))
    py = row.get("_py", "")
    ini = row.get("_ini", "")
    if not seg_n:
        return 0.0
    score = 0.0
    if seg_n in name_n:
        score = len(seg_n) * 2.0
    elif len(seg_n) >= 2 and any(
            name_n.startswith(seg_n[:i]) or seg_n[:i] in name_n
            for i in range(len(seg_n), 1, -1)):
        score = 2.0
    if seg_py:
        if seg_py in py:
            score = max(score, len(seg_py) * 1.5)
        elif len(seg_py) >= 2 and seg_py in ini:
            score = max(score, len(seg_py))
    return score


def _prefilter_catalog(text, customers, items, n_cust=50, n_items=60):
    """Trim the catalog to the rows most relevant to the transcript so the
    LLM prompt stays small (big latency win). Scored per segment; generous
    cutoffs keep the right row in almost all cases."""
    segments = _seg_list(text)
    scored = []
    for seg in segments:
        # strip qty before alias lookup ("jiji两瓶"/"两瓶jiji" -> "jiji" -> GG)
        name_part, _qty = _split_qty(seg)
        variants = [name_part]
        alias = _alias_for(name_part)
        if alias:
            variants.append(alias)
        scored.append([(_norm(v), _py_full(v)) for v in variants])

    def rank(rows, field):
        out = []
        for r in rows:
            s = max((_relevance(sn, sp, r, field)
                     for variants in scored for sn, sp in variants[:1]),
                    default=0.0)
            sa = max((_relevance(sn, sp, r, field)
                      for variants in scored for sn, sp in variants[1:]),
                     default=0.0)
            out.append((s, sa, r))
        out.sort(key=lambda x: -(x[0] + x[1]))
        top = [r for s, sa, r in out[:n_items] if s > 0 or sa > 0]
        # alias-matched rows always survive the cut
        top += [r for s, sa, r in out[n_items:] if sa > 0 and r not in top]
        return top or [r for _, _, r in out[:20]]

    return (rank(customers, "customer_name")[:n_cust],
            rank(items, "item_name"))


def _fast_parse(text, customers, items):
    """Instant local parse for clear-cut transcripts.
    Returns the result dict, or None when anything is ambiguous (then the
    caller falls back to the LLM)."""
    segments = _seg_list(text)
    if not segments:
        return None
    # pull shipping rule and freight out of the segment stream first
    shipping, freight, work = None, None, []
    for seg in segments:
        rule = _match_shipping(seg)
        if rule and shipping is None:
            shipping = rule
            continue
        amt = _match_freight(seg)
        if amt is not None and freight is None:
            freight = amt
            continue
        work.append(seg)
    segments = work
    if not segments:
        if shipping or freight:
            return {"customer": None, "customer_phrase": None, "items": [],
                    "unmatched": [], "notes": None,
                    "shipping_rule": shipping, "freight": freight}
        return None
    cust, cust_score, cust_i = None, 0, -1
    cust_candidates = None
    for idx, seg in enumerate(segments):
        sn, sp = _norm(seg), _py_full(seg)
        alias = _alias_for(seg)  # customer aliases too (样液 -> DC1M上海漾叶)
        best_here, bs_here = [], 0
        for c in customers:
            if alias and (c["name"] == alias
                          or c.get("customer_name") == alias):
                s = 25.0  # curated alias — beats fuzzy matches
            else:
                s = _relevance(sn, sp, c, "customer_name")
            if s > bs_here + 0.01:
                best_here, bs_here = [c], s
            elif abs(s - bs_here) <= 0.01 and s > 0:
                if all(c["name"] != x["name"] for x in best_here):
                    best_here.append(c)
        if bs_here > cust_score:
            cust, cust_score, cust_i = best_here[0], bs_here, idx
            cust_candidates = best_here if len(best_here) > 1 else None
    if not cust or cust_score < 4:
        return None
    out = []
    for idx, seg in enumerate(segments):
        if idx == cust_i:
            continue
        name_part, qty = _split_qty(seg)
        alias = _alias_for(name_part)
        if alias:
            name_part = alias
        if _norm(name_part) in _GENERIC_TERMS:
            return None  # too vague (just 雷司令 etc.) -> LLM decides
        sn, sp = _norm(name_part), _py_full(name_part)
        best, bs = None, 0
        for it in items:
            s = _relevance(sn, sp, it, "item_name")
            if sn and sn == _norm(it.get("item_code")):
                s = max(s, 20.0)
            # prefer the latest vintage when relevance is tied
            if s > bs + 0.01 or \
                    (best is not None and abs(s - bs) <= 0.01
                     and _vintage(it) > _vintage(best)):
                best, bs = it, s
        if not best or bs < 6:
            return None  # something we can't place -> LLM handles it
        # bare vineyard name tying across different traits (天梯园 -> GG vs
        # 珍藏 vs 晚摘 vs 串选)? ambiguous -> LLM with a clarifying note
        tied = [it for it in items
                if it is not best and bs > 0 and
                abs(_relevance(sn, sp, it, "item_name") - bs) <= 0.01]
        if len({_traits(it.get("item_name")) for it in [best, *tied]}) > 1:
            return None
        existing = next((x for x in out
                         if x["item_code"] == best["item_code"]), None)
        if existing:
            existing["qty"] += qty
            continue
        out.append({"item_code": best["item_code"],
                    "item_name": best.get("item_name", best["item_code"]),
                    "uom": best.get("stock_uom", ""),
                    "qty": qty, "phrase": name_part,
                    "rate": _price_for(best["item_code"], cust["name"])})
    if not out:
        return None
    result = {"customer": None if cust_candidates else
              {"name": cust["name"], "customer_name": cust["customer_name"]},
              "customer_phrase": segments[cust_i],
              "items": out, "unmatched": [], "notes": None,
              "shipping_rule": shipping, "freight": freight}
    if cust_candidates:
        result["customer_candidates"] = [
            {"name": c["name"], "customer_name": c["customer_name"]}
            for c in cust_candidates]
    return result


def _parse_transcript(text):
    """Shared pipeline: transcript text -> customer + items + notes.
    Fast local parse first; LLM (kimi/codex CLI) only for hard cases."""
    # whisper mishears 五瓶 as 物品 (wǔpíng/wùpǐn); the app never uses 物品
    text = text.replace("物品", "五瓶")
    with _cache_lock:
        all_customers = list(_cache["customers"])
        all_items = list(_cache["items"])

    fast = _fast_parse(text, all_customers, all_items)
    if fast is not None:
        return fast

    customers, items = _prefilter_catalog(text, all_customers, all_items)

    with _cache_lock:
        ship_rules = [r["name"] for r in _cache["shipping_rules"]]
    prompt = _PARSE_PROMPT.format(
        customers="、".join(c["customer_name"] for c in customers),
        items="；".join(f"{i['item_code']}|{i.get('item_name', '')}"
                        for i in items),
        learned=_learned_hint(),
        shipping="、".join(ship_rules))
    parsed = _llm_parse(prompt, text)

    # map LLM output back to real records (full catalog, not just prefiltered)
    customer = None
    if parsed.get("customer"):
        customer = next(
            (c for c in all_customers
             if c["customer_name"] == parsed["customer"]
             or c["name"] == parsed["customer"]), None)
    out_items, unmatched = [], []
    for it in parsed.get("items") or []:
        code = str(it.get("item_code") or "").strip()
        if not code:
            continue  # LLM returned an empty/placeholder row
        with _cache_lock:
            row = next((i for i in _cache["items"]
                        if i["item_code"] == code), None)
        if not row:
            unmatched.append(code)
            continue
        qty = float(it.get("qty") or 1)
        existing = next((x for x in out_items
                         if x["item_code"] == row["item_code"]), None)
        if existing:
            existing["qty"] += qty
            continue
        out_items.append({
            "item_code": row["item_code"],
            "item_name": row.get("item_name", row["item_code"]),
            "uom": row.get("stock_uom", ""),
            "qty": qty,
            "phrase": it.get("phrase", ""),
            "rate": _price_for(row["item_code"],
                               customer["name"] if customer else None),
        })

    ship = parsed.get("shipping_rule")
    if ship and ship not in ship_rules:
        # try alias matching if the LLM returned free text
        ship = _match_shipping(ship)
    freight = parsed.get("freight")
    try:
        freight = float(freight) if freight is not None else None
    except (TypeError, ValueError):
        freight = None

    return {
        "customer": customer and {"name": customer["name"],
                                  "customer_name": customer["customer_name"]},
        "customer_phrase": parsed.get("customer_phrase"),
        "items": out_items,
        "unmatched": unmatched,
        "notes": parsed.get("notes"),
        "shipping_rule": ship,
        "freight": freight,
    }


# ------------------------------------------------------- voice transcription
# Local faster-whisper (no API key). Model loads once in the background.

_whisper = {"model": None, "lock": threading.Lock()}


def _hotwords():
    """Domain vocabulary for whisper: distinctive item short-names (winery
    prefixes stripped, vineyard 园 names extracted), item codes, customer
    names — so 甜心犬/美鸭鸭/天梯园 are heard correctly.
    Whisper keeps the END of a long prompt, so customers go last."""
    codes, names = [], []
    with _cache_lock:
        items = list(_cache["items"])
        customers = list(_cache["customers"])
    for it in items:
        name = it.get("item_name") or ""
        if it.get("item_code"):
            codes.append(it["item_code"])
        lead = re.match(r"[一-鿿]{2,12}", name)
        if not lead:
            continue
        names.append(lead.group(0)[:8])          # 幸运甜心犬莫斯卡 / 美鸭鸭莫斯卡托
        stripped = re.sub(r"^[一-鿿]{2,6}酒庄", "", name)
        if stripped != name:
            m = re.match(r"[一-鿿]{2,8}", stripped)
            if m:
                names.append(m.group(0)[:8])     # 丹魄干红 / 雷司令干白
        # vineyard names the way people SAY them: 艾尔登村天梯园 -> 天梯园
        for m in re.finditer(r"[一-鿿]{2,5}园", name):
            short = re.sub(r"^.*村", "", m.group(0))
            names.append(short)                  # 天梯园 / 日晷园 / 森林园
            if "GG" in name:
                names.append(short + "GG")       # 天梯园GG / 森林园GG
    names += ["天梯园", "日晷园", "香料园", "森林园", "修士园", "修道院", "修道园",
              "灵犀园", "灵犀园晚摘", "金滴园",
              "甜心犬", "美鸭鸭", "森林之约", "凯瑟琳", "GG",
              "herman干白", "赫曼干白",
              "德邦", "顺丰", "冰袋", "隔热膜", "运费",
              "雷司令", "丹魄", "莫斯卡托", "阿尔巴利诺", "瓶", "箱"]
    cust_names = [c["customer_name"] for c in customers
                  if c.get("customer_name")]
    # whisper only keeps the END of a long prompt (~few hundred chars), so
    # order by importance: codes & customers first (may be dropped), the
    # spoken short forms last (always survive)
    names = list(dict.fromkeys(names))           # dedupe, keep order
    return "，".join(codes + cust_names + names)[-1600:]


def _whisper_model():
    if _whisper["model"] is None:
        with _whisper["lock"]:
            if _whisper["model"] is None:
                from faster_whisper import WhisperModel
                _whisper["model"] = WhisperModel(
                    "medium", device="cpu", compute_type="int8")
    return _whisper["model"]


threading.Thread(target=_whisper_model, daemon=True).start()


RECORDINGS_DIR = "recordings"


def _save_recording(tmp_path, suffix, text):
    """Keep every voice recording + transcript for later review/learning."""
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = os.path.join(RECORDINGS_DIR, f"{ts}{suffix}")
    # avoid same-second collisions
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(RECORDINGS_DIR, f"{ts}-{n}{suffix}")
        n += 1
    import shutil
    shutil.move(tmp_path, dest)
    with open(os.path.join(RECORDINGS_DIR, "index.jsonl"), "a") as f:
        f.write(json.dumps({"file": os.path.basename(dest), "text": text,
                            "ts": ts}, ensure_ascii=False) + "\n")
    return dest


@app.route("/api/parse_audio", methods=["POST"])
def parse_audio():
    """Audio upload -> whisper transcription -> LLM parse pipeline.
    Recordings are kept in recordings/ for later review/learning."""
    f = request.files.get("audio")
    if not f:
        return jsonify({"error": "audio file required"}), 400
    import tempfile
    suffix = os.path.splitext(f.filename or "")[1] or ".m4a"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        f.save(tmp.name)
    try:
        segments, _ = _whisper_model().transcribe(
            tmp.name, language="zh", beam_size=1, vad_filter=True,
            hotwords=_hotwords(),
            initial_prompt="葡萄酒销售订单，包含客户名称、商品名称和数量（瓶/箱）。")
        text = "".join(s.text for s in segments).strip()
        if not text:
            _save_recording(tmp.name, suffix, "")
            return jsonify({"error": "没听清，请再试一次"}), 422
        result = _parse_transcript(text)
        result["text"] = text
        _save_recording(tmp.name, suffix, text)
        return jsonify(result)
    except (RuntimeError, ValueError) as e:
        _save_recording(tmp.name, suffix, f"[error] {e}")
        return jsonify({"error": f"解析服务不可用:{e}"}), 502
    except Exception as e:  # noqa: BLE001
        _save_recording(tmp.name, suffix, f"[error] {e}")
        return jsonify({"error": f"识别失败:{e}"}), 500


@app.route("/api/deliveries")
def deliveries():
    r = erp.call("GET", "/api/resource/Delivery Note", params={
        "fields": json.dumps(["name", "customer_name", "posting_date",
                              "status", "docstatus", "grand_total"]),
        "order_by": "posting_date desc",
        "limit_page_length": 100,
    })
    body, code = erp_json(r)
    return jsonify(body.get("data", body) if code == 200 else body), code


@app.route("/api/deliveries/<path:name>")
def delivery_detail(name):
    r = erp.call("GET", f"/api/resource/Delivery Note/{name}")
    body, code = erp_json(r)
    return jsonify(body.get("data", body) if code == 200 else body), code


@app.route("/api/orders/<path:name>/make_delivery", methods=["POST"])
def make_delivery(name):
    r = erp.call("POST",
                 "/api/method/erpnext.selling.doctype.sales_order"
                 ".sales_order.make_delivery_note",
                 data={"source_name": name})
    body, code = erp_json(r)
    if code != 200:
        return jsonify(body), code
    doc = body["message"]
    r2 = erp.call("POST", "/api/resource/Delivery Note", json=doc)
    body2, code2 = erp_json(r2)
    return jsonify(body2.get("data", body2) if code2 == 200 else body2), code2


@app.route("/api/deliveries/<path:name>", methods=["PUT"])
def update_delivery(name):
    payload = request.get_json(force=True)
    # child rows must be echoed back in full; client sends complete rows
    r = erp.call("PUT", f"/api/resource/Delivery Note/{name}",
                 json={"items": payload.get("items", [])})
    body, code = erp_json(r)
    return jsonify(body.get("data", body) if code == 200 else body), code


@app.route("/api/deliveries/<path:name>/submit", methods=["POST"])
def submit_delivery(name):
    r = erp.call("PUT", f"/api/resource/Delivery Note/{name}",
                 json={"docstatus": 1})
    body, code = erp_json(r)
    return jsonify(body.get("data", body) if code == 200 else body), code


@app.route("/api/pdf")
def pdf():
    doctype = request.args.get("doctype")
    name = request.args.get("name")
    if doctype not in PRINT_FORMATS or not name:
        return jsonify({"error": "bad doctype/name"}), 400
    try:
        data = render_pdf(doctype, name)
    except Exception as e:  # noqa: BLE001 - surface any render failure to UI
        return jsonify({"error": f"PDF 生成失败: {e}"}), 500
    # download=1 -> real file the phone can share to WeChat etc.
    mode = "attachment" if request.args.get("download") else "inline"
    return Response(data, mimetype="application/pdf", headers={
        "Content-Disposition": f'{mode}; filename="{name}.pdf"'})


# ------------------------------------------------------- stock alerts

ALERTS_FILE = "alerts_state.json"
LOW_STOCK_THRESHOLD = 60  # bottles, total across all warehouses


def _load_alert_state():
    try:
        with open(ALERTS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_alert_state(state):
    with open(ALERTS_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False)


def _disarm_active(entry):
    if not entry:
        return False
    if entry.get("until") == "forever":
        return True
    until = entry.get("until")
    return bool(until) and until >= datetime.date.today().isoformat()


@app.route("/api/stock_alerts")
def stock_alerts():
    # total stock per item across all warehouses (live from the ERP)
    r = erp.call("GET", "/api/resource/Bin", params={
        "fields": json.dumps(["item_code", "actual_qty"]),
        "limit_page_length": 5000,
    })
    body, code = erp_json(r)
    if code != 200:
        return jsonify(body), code
    totals = {}
    for b in body["data"]:
        totals[b["item_code"]] = totals.get(b["item_code"], 0) + \
            float(b.get("actual_qty") or 0)
    state = _load_alert_state()
    with _cache_lock:
        items = {it["item_code"]: it for it in _cache["items"]}
    alerts = []
    for code, qty in totals.items():
        if qty >= LOW_STOCK_THRESHOLD or code not in items:
            continue
        entry = state.get(code)
        alerts.append({
            "item_code": code,
            "item_name": items[code].get("item_name", code),
            "total_qty": qty,
            "disarmed": _disarm_active(entry),
            "disarmed_until": (entry or {}).get("until"),
        })
    alerts.sort(key=lambda a: a["total_qty"])
    return jsonify({"threshold": LOW_STOCK_THRESHOLD, "alerts": alerts})


@app.route("/api/stock_alerts/disarm", methods=["POST"])
def disarm_alert():
    payload = request.get_json(force=True)
    code = payload.get("item_code")
    if not code:
        return jsonify({"error": "item_code required"}), 400
    if payload.get("mode") == "forever":
        until = "forever"
    else:
        days = int(payload.get("days") or 30)
        until = (datetime.date.today()
                 + datetime.timedelta(days=days)).isoformat()
    state = _load_alert_state()
    state[code] = {"until": until}
    _save_alert_state(state)
    return jsonify({"item_code": code, "until": until})


@app.route("/api/stock_alerts/rearm", methods=["POST"])
def rearm_alert():
    payload = request.get_json(force=True)
    code = payload.get("item_code")
    state = _load_alert_state()
    state.pop(code, None)
    _save_alert_state(state)
    return jsonify({"item_code": code, "until": None})


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    # NOTE: user requirement — never use port 8000, always this random port.
    port = int(os.environ.get("PORT", 8347))
    print(f"nextERP mobile app -> http://{lan_ip()}:{port}  (open on your phone)")
    app.run(host="0.0.0.0", port=port, threaded=True)
