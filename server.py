"""Mobile-friendly mini-app for nextERP (ERPNext/Frappe).

Serves a mobile-first web UI for 销售订单 (Sales Order) and 销售出货 (Delivery
Note), plus PDF printing rendered locally with Playwright (server-side PDF on
the ERP is broken).

Usage:  bin/python server.py   then open http://<LAN-IP>:8347 on your phone.

Credentials are read from .env and stay server-side; the phone only ever
talks to this Flask app.
"""
import datetime
import difflib
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
          "customer_groups": [], "territories": [], "shipping_rules": [],
          "recent_orders": []}
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
    "recent_orders": {
        "path": "/api/resource/Sales Order",
        "params": {
            "fields": json.dumps(["customer"]),
            "order_by": "creation desc",
            "limit_page_length": 5000,
        },
    },
}


def _cache_file(kind):
    return os.path.join(CACHE_DIR, f"{kind}.json")


_PY_CACHE = {}
_PYL_CACHE = {}


def _py_full(s):
    """Lowercase no-tone pinyin of s, spaces stripped (ASCII passes through).
    Cached: the voice parser asks for the same catalog names repeatedly."""
    key = str(s or "")
    if key not in _PY_CACHE:
        from pypinyin import pinyin, Style
        _PY_CACHE[key] = "".join(p[0] for p in pinyin(
            key, style=Style.NORMAL)).replace(" ", "").lower()
    return _PY_CACHE[key]


def _py_syllables(s):
    """s as a tuple of no-tone pinyin syllables (cached). Only letter tokens
    are kept — punctuation/digits would break syllable-contiguity checks.
    Nasal -ng finals are normalized away (jing~jin, chang~chan): the most
    common Chinese ASR confusion, and not distinctive between customers."""
    key = str(s or "")
    if key not in _PYL_CACHE:
        from pypinyin import pinyin, Style
        tokens = []
        for p in pinyin(key, style=Style.NORMAL):
            t = p[0].strip().lower()
            if not t.isalpha():
                continue
            if t.endswith("ng") and len(t) > 2:
                t = t[:-1]
            tokens.append(t)
        _PYL_CACHE[key] = tuple(tokens)
    return _PYL_CACHE[key]


def _py_initials(s):
    from pypinyin import pinyin, Style
    return "".join(p[0] for p in pinyin(
        str(s or ""), style=Style.FIRST_LETTER)).replace(" ", "").lower()


_CUSTOMER_CATEGORY_RE = re.compile(
    r"^(?:OLC1W|DC1M|DC1W|DCIM|RIC|EIC|VIP|OL|OT|D|E|S)\s*",
    re.IGNORECASE)


def _spoken_customer_name(name):
    """Customer name as people say it, without internal ERP categories."""
    return _CUSTOMER_CATEGORY_RE.sub("", str(name or "")).strip()


CUSTOMER_SPOKEN_NAMES_FILE = "customer_spoken_names.txt"


def _load_customer_spoken_names():
    """Read the local, user-maintained customer voice dictionary."""
    aliases = {}
    try:
        with open(CUSTOMER_SPOKEN_NAMES_FILE, encoding="utf-8") as f:
            current_id = None
            for raw in f:
                line = raw.strip()
                if line.startswith("customer_id ="):
                    current_id = line.split("=", 1)[1].strip()
                    aliases.setdefault(current_id, [])
                elif line.startswith("spoken_name =") and current_id:
                    spoken = line.split("=", 1)[1].strip()
                    if spoken and spoken not in aliases[current_id]:
                        aliases[current_id].append(spoken)
    except OSError:
        return {}
    return aliases


def _annotate(kind, rows):
    """Attach pinyin search fields (_py full, _ini initials) to cached rows."""
    field = {"customers": "customer_name", "items": "item_name"}.get(kind)
    if not field:
        return rows
    spoken_names = _load_customer_spoken_names() if kind == "customers" else {}
    for r in rows:
        r["_py"] = _py_full(r.get(field))
        r["_ini"] = _py_initials(r.get(field))
        if kind == "customers":
            spoken = _spoken_customer_name(r.get(field))
            voice_names = list(dict.fromkeys(
                spoken_names.get(r.get("name"), []) + [spoken]))
            r["_spoken"] = spoken
            r["_spoken_norm"] = spoken.lower().replace(" ", "")
            r["_spoken_py"] = _py_full(spoken)
            r["_voice_names"] = voice_names
            r["_voice_norms"] = [x.lower().replace(" ", "")
                                 for x in voice_names]
            r["_voice_pys"] = [_py_full(x) for x in voice_names]
            r["_voice_inis"] = [_py_initials(x) for x in voice_names]
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
        if kind == "recent_orders":
            # Recency is a customer-ID tie-breaker, so a fixed recent window
            # is insufficient: two old IDs could both fall outside it. Fetch
            # every lightweight {customer} row, newest first, in pages.
            rows, start = [], 0
            page_length = spec["params"]["limit_page_length"]
            while True:
                params = dict(spec["params"], limit_start=start)
                r = erp.call("GET", spec["path"], params=params)
                r.raise_for_status()
                page = r.json().get("data", [])
                rows.extend(page)
                if len(page) < page_length:
                    break
                start += len(page)
        else:
            r = erp.call("GET", spec["path"], params=spec["params"])
            r.raise_for_status()
            rows = r.json().get("data", [])
        rows = _annotate(kind, rows)
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


def _customer_recent_order_rank():
    """Customer ID -> position of its newest appearance in ERP orders.

    recent_orders is fetched newest-first. This is the deterministic
    tie-breaker when two spoken names have identical pinyin.
    """
    with _cache_lock:
        recent = list(_cache["recent_orders"])
    rank = {}
    for index, order in enumerate(recent):
        customer = order.get("customer")
        if customer and customer not in rank:
            rank[customer] = index
    return rank

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
    if code != 200:
        _log_api_error(f"POST /api/orders/{name}/submit",
                       {"docstatus": 1}, code, body)
    return jsonify(body.get("data", body)
                   if code == 200 else _friendly_error(body)), code


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
    row = _annotate("customers", [{"name": created["name"],
           "customer_name": created["customer_name"],
           "default_price_list": created.get("default_price_list")}])[0]
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
    if any(not it.get("item_code") for it in items):
        return jsonify({"error": "有商品缺少货号，请重新选择"}), 400
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
            # 赠品 rows: ERP treats a falsy rate as "missing" and silently
            # refills it from the price list. price_list_rate 0 +
            # is_free_item makes the zero price stick.
            **({"price_list_rate": 0, "is_free_item": 1}
               if (it.get("is_free") or not float(it.get("rate") or 0))
               else {}),
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
        _log_api_error("POST /api/orders", doc, code, body)
        return jsonify(_friendly_error(body)), code
    created = body["data"]
    if payload.get("submit"):
        r2 = erp.call("PUT", f"/api/resource/Sales Order/{created['name']}",
                      json={"docstatus": 1})
        body2, code2 = erp_json(r2)
        if code2 != 200:
            _log_api_error(f"POST /api/orders(submit) {created['name']}",
                           {"docstatus": 1}, code2, body2)
            # order exists as draft — tell the user so it isn't lost
            friendly = _friendly_error(body2).get("error")
            return jsonify({"error": f"订单已保存为草稿 {created['name']}，"
                                     f"但提交失败：{friendly}",
                            "name": created["name"]}), code2
        created = body2["data"]
    return jsonify(created)


_FIELD_LABELS = {
    "charge_type": "费用类型（费用/税费行）",
    "account_head": "会计科目（费用/税费行）",
    "customer": "客户",
    "item_code": "商品货号",
    "items": "商品行",
    "delivery_date": "送货日期",
    "transaction_date": "订单日期",
    "selling_price_list": "价格表",
    "set_warehouse": "仓库",
}


def _friendly_error(body):
    """Translate ERP error gibberish into an actionable Chinese message.
    MandatoryError: [Sales Order, X]: charge_type -> 缺少必填项：费用类型…"""
    err = (body or {}).get("error") or ""
    m = re.search(r"MandatoryError: \[[^]]+\]:\s*(.+)", err)
    if m:
        fields = [_FIELD_LABELS.get(f.strip(), f.strip())
                  for f in m.group(1).split(",")]
        return {"error": "缺少必填项：" + "、".join(fields)}
    return body


def _log_api_error(endpoint, payload, code, body):
    """Append failed ERP writes to api_errors.jsonl so the next
    '提交失败' is debuggable from the server side."""
    try:
        with open("api_errors.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "endpoint": endpoint, "payload": payload,
                "code": code, "error": body.get("error") or str(body)[:500],
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


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
        taxes = []
        for row in payload["taxes"]:
            # charge_type is mandatory on Sales Taxes and Charges rows; rows
            # added in the app editor don't carry it. Actual = fixed amount.
            row.setdefault("charge_type", "Actual")
            if row.get("account_head"):
                taxes.append(row)
        doc["taxes"] = taxes
    if not doc:
        return jsonify({"error": "nothing to update"}), 400
    r = erp.call("PUT", f"/api/resource/Sales Order/{name}", json=doc)
    body, code = erp_json(r)
    if code != 200:
        _log_api_error(f"PUT /api/orders/{name}", doc, code, body)
    return jsonify(body.get("data", body)
                   if code == 200 else _friendly_error(body)), code


@app.route("/api/customer_delivery_meta")
def customer_delivery_meta():
    """Addresses and contacts linked to a Customer for Delivery Note setup."""
    customer = (request.args.get("customer") or "").strip()
    if not customer:
        return jsonify({"error": "customer required"}), 400
    link_filters = [["Dynamic Link", "link_doctype", "=", "Customer"],
                    ["Dynamic Link", "link_name", "=", customer]]
    address_r = erp.call("GET", "/api/resource/Address", params={
        "fields": json.dumps(["name", "address_title", "address_type",
                              "address_line1", "city", "is_shipping_address"]),
        "filters": json.dumps(link_filters + [["disabled", "=", 0]]),
        "order_by": "is_shipping_address desc",
        "limit_page_length": 100,
    })
    address_body, address_code = erp_json(address_r)
    if address_code != 200:
        return jsonify(address_body), address_code
    contact_r = erp.call("GET", "/api/resource/Contact", params={
        "fields": json.dumps(["name", "first_name", "last_name",
                              "is_primary_contact", "mobile_no", "phone",
                              "email_id"]),
        "filters": json.dumps(link_filters),
        "order_by": "is_primary_contact desc",
        "limit_page_length": 100,
    })
    contact_body, contact_code = erp_json(contact_r)
    if contact_code != 200:
        return jsonify(contact_body), contact_code
    return jsonify({"addresses": address_body.get("data", []),
                    "contacts": contact_body.get("data", [])})


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
# LLM backends: kimi/codex CLIs (subscription logins) are primary — on
# badly misheard transcripts they clearly beat the local 1.7B. "local"
# comes next: Qwen3-1.7B behind the llama-cli-wrapper.service gateway on
# 127.0.0.1:8349 (installed by ../LLMqwen17/install.sh). The gateway starts
# llama-server on the first request and kills it after 5 min idle, so the
# model never sits in RAM/VRAM — we just POST and forget. "claude" (CLI)
# is the final fallback.

VOICE_LLM_BACKENDS = ["kimi", "codex", "local", "claude"]

_PARSE_SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "voice-parse-schema.json")

_LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://127.0.0.1:8349")


def _local_llm_parse(full_prompt):
    """Local Qwen3-1.7B via the on-demand gateway (first call pays model
    load, ~10s; warm calls ~3s). Schema-constrained JSON, thinking off."""
    with open(_PARSE_SCHEMA) as f:
        schema = json.load(f)
    r = requests.post(
        _LOCAL_LLM_URL + "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": full_prompt
                          + "\n\n只输出一个JSON对象，不要任何其他文字。 /no_think"}],
            "temperature": 0,
            "max_tokens": 600,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "order", "strict": True, "schema": schema}},
        },
        timeout=180)  # first call includes llama-server startup + model load
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


def _claude_parse(full_prompt):
    import subprocess
    r = subprocess.run(
        ["claude", "-p", full_prompt
         + "\n\n只输出一个JSON对象，不要任何其他文字、解释或标记。"],
        cwd=os.path.dirname(os.path.abspath(__file__)),
        capture_output=True, text=True, timeout=240, check=False)
    m = re.search(r"\{.*\}", r.stdout, re.S)
    if not m:
        raise RuntimeError(f"claude 输出中无 JSON: {r.stdout[-200:]}")
    return json.loads(m.group(0))


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
    """Returns (parsed_dict, backend_name)."""
    full_prompt = prompt + "\n\n口述订单:" + user_text
    errors = []
    for backend in VOICE_LLM_BACKENDS:
        fn = {"local": _local_llm_parse, "kimi": _kimi_parse,
              "codex": _codex_parse, "claude": _claude_parse}[backend]
        try:
            return fn(full_prompt), backend
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
    """Render learned aliases as a prompt section (empty string if none).
    Capped: the harvest collects hundreds of entries, which would blow the
    local model's context — most recent entries are the most relevant."""
    data = _load_learned()
    lines = []
    for norm, a in data.get("items", {}).items():
        lines.append(f"「{a['phrase']}」= {a['item_code']}({a.get('item_name', '')[:20]})")
    for norm, a in data.get("customers", {}).items():
        lines.append(f"「{a['phrase']}」= 客户 {a['customer']}")
    if not lines:
        return ""
    lines = lines[-60:]
    return ("\n用户纠正习惯（最高优先级，必须遵循）:\n" + "\n".join(lines) + "\n")


_PARSE_PROMPT = """你是葡萄酒销售订单的语音解析助手。用户口述一张订单，你要提取客户和商品明细。

客户列表（名称|拼音，口述常有同音字，先把听到的词转拼音再比对）:
{customers}

商品列表（货号|名称|口语名拼音）:
{items}
{learned}
规则：
- 客户：从客户列表中选最匹配的一个。注意同音字（如"一杯"="壹杯")、简称（如"万杯")。带☆的是最近有ERP订单的客户，按订单新旧排在前；几个客户读音相同或相近时（如两个"漾叶")，必须选带☆的，多个带☆时选排最前的。
- 商品：匹配货号或名称关键词。注意口语别名："jiji/JJ/吉吉"=GG雷司令。同音字也可能出现。
- 数量：中文或阿拉伯数字，单位瓶/箱等，可能在品名前面或后面。一箱=12瓶。没说数量默认1瓶。
- 年份：口述中提到年份（如"2025年"、"二零二五年"）时，必须选对应年份的货号；未提到时选最新年份（如同时有22和25，选25)。
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
        phrase, _pq = _split_qty(phrase)  # learn the name, not "8瓶灵犀园"
        if not phrase:
            continue
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


# ------------------------------------------------------- voice parse logging
# Set False later, when the pipeline is trusted. Logs every parse to
# voice_log.jsonl for review/learning analysis.
DEBUG_VOICE = True
VOICE_LOG = "voice_log.jsonl"


def _vlog(entry):
    if not DEBUG_VOICE:
        return
    try:
        with open(VOICE_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


@app.route("/api/parse_order", methods=["POST"])
def parse_order():
    """Parse a dictated order via Kimi into customer + items (with prices)."""
    text = (request.get_json(force=True).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    import time
    t0 = time.time()
    try:
        result = _parse_transcript(text)
        _vlog({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "src": "text", "text": text, "path": result.get("_path"),
               "ms": round((time.time() - t0) * 1000),
               "customer": (result.get("customer") or {}).get("customer_name"),
               "items": [[i["item_code"], i["qty"]] for i in result.get("items", [])],
               "shipping": result.get("shipping_rule"),
               "freight": result.get("freight")})
        return jsonify(result)
    except (RuntimeError, ValueError) as e:
        _vlog({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "src": "text", "text": text, "error": str(e)[:200]})
        return jsonify({"error": f"解析服务不可用:{e}"}), 502
    except Exception as e:  # noqa: BLE001 - e.g. subprocess timeout
        _vlog({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "src": "text", "text": text, "error": str(e)[:200]})
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
    r"^(?:(?:运费|快递费?)([0-9]+(?:\.[0-9]+)?|[零一二两三四五六七八九十]{1,4})元?"
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
_QTY_START = re.compile(
    r"([0-9]+|[零一二两三四五六七八九十]{1,3})\s*"
    r"(瓶|箱|盒|个|支|件|听)")


def _customer_prefix(phrase):
    """Longest known customer voice name prefixing the phrase (hanzi, or
    pinyin for mishearings like 样页~漾叶). Keeps the heard form."""
    with _cache_lock:
        customers = list(_cache["customers"])
    phrase_py = _py_full(phrase)
    best = None
    for c in customers:
        for name, py in zip(c.get("_voice_names", []),
                            c.get("_voice_pys", [])):
            if len(name) < 2:
                continue
            if phrase.startswith(name) or (py and phrase_py.startswith(py)):
                if best is None or len(name) > len(best):
                    best = name
    return phrase[:len(best)] if best else None


def _customer_voice_phrase(text):
    """Customer prefix from either a paused or continuous spoken order."""
    first = _SPLIT_RE.split(str(text or ""), maxsplit=1)[0].strip()
    qty = _QTY_START.search(first)
    if qty:
        first = first[:qty.start()].strip()
    first = re.sub(r"^(?:给|帮|客户)\s*", "", first)
    # item-before-quantity orders (漾叶要日晷园三瓶 / 漾叶日晷园三瓶): the
    # customer is what precedes the verb, or a known voice-name prefix
    verb = re.search(r"(?:下单|订购|购买|要|来买|来|买)", first)
    if verb:
        first = first[:verb.start()]
    else:
        first = _customer_prefix(first) or first
    first = re.sub(r"(?:下单|订购|购买|买|要|来)\s*$", "", first)
    return first.strip()


def _seg_list(text):
    """Split a transcript, keeping bare qty tokens ('五瓶', '2') glued to the
    preceding segment ('阿尔巴利诺 五瓶' -> ['一杯','阿尔巴利诺五瓶'])."""
    raw = [s.strip() for s in _SPLIT_RE.split(text) if s.strip()]
    # Split quantity clauses even when Whisper mixes punctuation and a
    # continuous sentence. A standalone vintage (", 2025年,") belongs to the
    # preceding item rather than becoming an unmatched segment.
    expanded = []
    for index, segment in enumerate(raw):
        if re.fullmatch(r"(?:19|20)\d{2}年?", segment) and expanded:
            expanded[-1] += segment
            continue
        segment = re.sub(r"^(?:和|及|再来|再要|再加|还有|然后|另外)",
                         "", segment).strip()
        qty_matches = list(_QTY_START.finditer(segment))
        if not qty_matches:
            expanded.append(segment)
            continue
        if index == 0 and qty_matches[0].start() > 0:
            customer = _customer_voice_phrase(segment)
            if customer:
                expanded.append(customer)
                # item may precede the qty (漾叶日晷园三瓶): keep the words
                # between customer/verb and the quantity as the item name;
                # qty-first (漾叶要三瓶日晷园) drops the customer head.
                head = segment[:qty_matches[0].start()]
                item_lead = head.split(customer, 1)[-1] \
                    if customer in head else ""
                item_lead = re.sub(
                    r"^(?:下单|订购|购买|要|来买|来|买)", "", item_lead).strip()
                segment = item_lead + segment[qty_matches[0].start():]
                qty_matches = list(_QTY_START.finditer(segment))
        # the split below separates qty tokens and the glue step reattaches
        # them, so both 三瓶日晷园 and 日晷园三瓶 keep the item name intact
        item_text = segment
        if index > 0:
            item_text = re.sub(r"^(?:下单|订购|购买|要|来买|来|买)", "",
                               item_text)
        item_text = re.sub(
            r"(?:和|及|再来|再要|再加|还有|然后|另外)\s*"
            r"(?=[0-9零一二两三四五六七八九十]{1,3}\s*"
            r"(?:瓶|箱|盒|个|支|件|听))", "", item_text)
        expanded.extend(s.strip() for s in re.split(
            r"(?<![0-9零一二两三四五六七八九十])"
            r"(?=[0-9零一二两三四五六七八九十]{1,3}\s*"
            r"(?:瓶|箱|盒|个|支|件|听))", item_text) if s.strip())
    raw = expanded
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


_YEAR_NUM_RE = re.compile(r"(20\d{2})\s*年?")
_YEAR_CN_RE = re.compile(r"([零〇一二三四五六七八九]{4})\s*年")
_YEAR_CN = {"零": "0", "〇": "0", "一": "1", "二": "2", "三": "3", "四": "4",
            "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}


def _spoken_vintage(seg):
    """Explicitly spoken vintage year (小海龙2025年 / 小海龙二零二五年)
    -> 2025, else None. Rule: spoken vintage wins; unspoken -> newest."""
    m = _YEAR_NUM_RE.search(seg)
    if m:
        return int(m.group(1))
    m = _YEAR_CN_RE.search(seg)
    if m:
        digits = "".join(_YEAR_CN.get(ch, "") for ch in m.group(1))
        if digits.startswith("20"):
            return int(digits)
    return None


def _item_family(item_code, items):
    """All cached rows in the same code family (GH018-18H/GH018-23H are one
    product/format family; trailing H kept — half vs standard bottle)."""
    item_code = str(item_code or "")
    match = re.match(r"^(.*)-(\d{2})(H?)$", item_code, re.I)
    if not match:
        exact = [row for row in items if row.get("item_code") == item_code]
        if exact:
            return exact
        # A voice alias may deliberately name only a family (ES022).
        return [row for row in items if re.match(
            rf"^{re.escape(item_code)}-\d{{2}}(?:H)?$",
            str(row.get("item_code") or ""), re.I)]
    family = (match.group(1).lower(), match.group(3).lower())
    return [row for row in items
            if (m2 := re.match(r"^(.*)-(\d{2})(H?)$",
                               str(row.get("item_code") or ""), re.I))
            and (m2.group(1).lower(), m2.group(3).lower()) == family]


def _vintage_match_item(item_code, year, items):
    """Family member with the explicitly spoken vintage, or None."""
    return next((row for row in _item_family(item_code, items)
                 if _vintage(row) == year), None)


def _latest_vintage_item(item_code, items):
    """Newest item in the same code family when vintage is unspoken."""
    candidates = _item_family(item_code, items)
    return max(candidates, key=_vintage) if candidates else None


def _lcs_len(a, b):
    """Length of the longest common substring of a and b."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


# Business-suffix words carry no matching signal (every other customer is a
# 酒庄/酒业/商贸), yet they dominate pinyin substring/LCS scores and cause
# silent misroutes: 分多酒庄 -> D黑皮诺酒庄 via shared "jiuzhuang", or a bare
# 葡萄酒 segment substring-matching 醺葡醄酒业 ("putaojiu" in its pinyin).
# Strip them from both sides so scoring hinges on the distinctive part.
# Order matters: business suffixes first, 葡萄酒 last (薰葡萄酒业 must lose
# 酒业 first, then 葡萄酒), and repeat until stable for adjacent combos.
_GENERIC_MATCH_WORDS = ("有限公司", "文化传播", "酒业", "商贸", "贸易",
                        "酒庄", "餐饮", "管理", "食品", "文化", "葡萄酒")


def _strip_generic(s):
    while True:
        stripped = s
        for word in _GENERIC_MATCH_WORDS:
            stripped = stripped.replace(word, "")
        if stripped == s:
            return s
        s = stripped


def _lcs_syl_chars(a, b):
    """Longest common contiguous run over syllable tuples ->
    (count, chars, start_in_a). Alignment is syllable-level so pinyin
    matches cannot cross syllable boundaries ("ngdian" inside wukangdian
    ≈ 名典 mingdian); scoring uses the character length of the matched
    syllables to stay on the old scale."""
    best = (0, 0, 0)
    prev_c = [0] * (len(b) + 1)
    prev_h = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur_c = [0] * (len(b) + 1)
        cur_h = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur_c[j] = prev_c[j - 1] + 1
                cur_h[j] = prev_h[j - 1] + len(a[i - 1])
                cand = (cur_c[j], cur_h[j], i - cur_c[j])
                if cand[:2] > best[:2]:
                    best = cand
        prev_c, prev_h = cur_c, cur_h
    return best


def _relevance(seg_n, seg_py, row, name_field):
    """Quick relevance of a transcript segment to a catalog row.
    Substring matches score highest; longest-common-substring on hanzi and
    pinyin absorbs ASR drift (幻影干魂葡道酒 ≈ 幻影干红葡萄酒)."""
    name_n = _norm(row.get(name_field))
    py = row.get("_py", "")
    ini = row.get("_ini", "")
    if not seg_n:
        return 0.0
    seg_d, name_d = _strip_generic(seg_n), _strip_generic(name_n)
    if (seg_d, name_d) != (seg_n, name_n):
        # rescore on the distinctive parts; a side that is ALL generic
        # words carries no signal at all
        if not seg_d or not name_d:
            return 0.0
        seg_n, name_n = seg_d, name_d
        seg_py = _py_full(seg_d)
        py = _py_full(name_d)
        ini = ""  # initials of the full name no longer match the stripped seg
    score = 0.0
    if seg_n in name_n:
        score = len(seg_n) * 2.0
    else:
        score = _lcs_len(seg_n, name_n) * 1.0
    if seg_py:
        if seg_py in py:
            score = max(score, len(seg_py) * 1.5)
        elif ini and len(seg_py) >= 2 and seg_py in ini:
            score = max(score, len(seg_py))
        else:
            if name_field == "item_name":
                # items match an isolated name part — char-level LCS absorbs
                # ASR drift (幻影干魂葡道酒 ≈ 幻影干红葡萄酒)
                score = max(score, _lcs_len(seg_py, py) * 0.9)
            else:
                # customers: syllable-level LCS (no cross-boundary fake
                # matches), scored by matched character length. The segment
                # carries verb+item noise, so a real match must cover most
                # of the voice name's syllables — a miss falls back to the
                # LLM instead of silently picking the wrong customer.
                # Exception: orders start with the customer, so a match
                # anchored at the very start of the segment (雄敬≈熊进) is
                # accepted on its own strength.
                seg_syl = _py_syllables(seg_n)
                name_syl = _py_syllables(name_n)
                count, chars, start = _lcs_syl_chars(seg_syl, name_syl)
                if name_syl and (count / len(name_syl) >= 0.6
                                 or start == 0):
                    score = max(score, chars * 0.9)
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
            if field == "customer_name":
                # also score spoken/voice names — a badly misheard transcript
                # may share nothing with the ERP customer_name while matching
                # the spoken form by pinyin (微唇 ~ 威醇 voice name)
                for v, vpy, vini in zip(r.get("_voice_names", []),
                                        r.get("_voice_pys", []),
                                        r.get("_voice_inis", [])):
                    vrow = {"voice": v, "_py": vpy, "_ini": vini}
                    s = max(s, max((_relevance(sn, sp, vrow, "voice")
                                    for variants in scored
                                    for sn, sp in variants[:1]),
                                   default=0.0))
            out.append((s, sa, r))
        out.sort(key=lambda x: -(x[0] + x[1]))
        top = [r for s, sa, r in out[:n_items] if s > 0 or sa > 0]
        # alias-matched rows always survive the cut
        top += [r for s, sa, r in out[n_items:] if sa > 0 and r not in top]
        return top or [r for _, _, r in out[:20]]

    return (rank(customers, "customer_name")[:n_cust],
            rank(items, "item_name"))


def _branch_customer_rescue(text, segments, customers):
    """Rescue branch customers whose head Whisper mishears (葡萄/普道 for
    葡道). Orders start with the customer, so: grep the branch keyword's
    pinyin in the full-transcript pinyin (homophone branches like
    闪康礼店/陕康里店 share pinyin and still match), and fuzzy-match the head
    against the leading customer phrase, also by pinyin.
    Returns (customer_row, branch_hanzi) or None."""
    phrase = _customer_voice_phrase(text) or (segments[0] if segments else "")
    phrase_py = _py_full(phrase)
    text_py = _py_full(text)
    if not phrase_py or not text_py:
        return None
    recent_rank = _customer_recent_order_rank()
    scored = []
    for c in customers:
        for voice in (c.get("_voice_names") or []):
            head, sep, branch = voice.partition("-")
            if not sep or not branch:
                continue
            head_py, branch_py = _py_full(head), _py_full(branch)
            if len(branch_py) < 4:
                continue
            # branch may be split around the head ("北京普道店"): accept the
            # branch core + 店 anywhere in the transcript pinyin
            hit = branch_py in text_py
            if not hit and branch_py.endswith("dian"):
                core = branch_py[:-4]
                hit = len(core) >= 4 and core in text_py \
                    and "dian" in text_py
            if not hit:
                continue
            if head_py and head_py in phrase_py:
                score = 1.0  # head heard correctly, just reordered
            else:
                score = max(
                    difflib.SequenceMatcher(None, phrase_py, head_py).ratio(),
                    difflib.SequenceMatcher(
                        None, phrase_py[:len(head_py)], head_py).ratio())
            if score >= 0.6:
                scored.append((score, c, branch, branch_py))
    if not scored:
        return None
    # shared spoken name -> the customer with the newest ERP order
    scored.sort(key=lambda row: (
        -row[0], recent_rank.get(row[1]["name"], float("inf"))))
    _, customer, branch, branch_py = scored[0]
    return customer, branch, branch_py


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
                s = max(
                    [_relevance(sn, sp, c, "customer_name")] + [
                        _relevance(sn, sp, {
                            "voice": voice,
                            "_py": voice_py,
                            "_ini": voice_ini,
                        }, "voice")
                        for voice, voice_py, voice_ini in zip(
                            c.get("_voice_names", []),
                            c.get("_voice_pys", []),
                            c.get("_voice_inis", []))])
            if s > bs_here + 0.01:
                best_here, bs_here = [c], s
            elif abs(s - bs_here) <= 0.01 and s > 0:
                if all(c["name"] != x["name"] for x in best_here):
                    best_here.append(c)
        if bs_here > cust_score:
            recent_rank = _customer_recent_order_rank()
            best_here.sort(key=lambda row: recent_rank.get(
                row["name"], float("inf")))
            # Several ERP customer IDs may intentionally share a spoken name
            # (including homophones such as 三年间/叁年间). An exact pinyin
            # match is not ambiguous: use the ID seen in the newest ERP order.
            exact_pinyin = [row for row in best_here if sp and
                            sp in (row.get("_voice_pys") or [])]
            if exact_pinyin:
                exact_pinyin.sort(key=lambda row: recent_rank.get(
                    row["name"], float("inf")))
                best_here = exact_pinyin[:1]
            cust, cust_score, cust_i = best_here[0], bs_here, idx
            cust_candidates = best_here if len(best_here) > 1 else None
    if not cust or cust_score < 4:
        # Customer head may be misheard (葡萄 for 葡道) while the branch
        # keyword survives; try pinyin head-match + branch grep before
        # giving up to the LLM.
        rescued = _branch_customer_rescue(text, segments, customers)
        if not rescued:
            return None
        cust, branch, branch_py = rescued
        # The head phrase may have absorbed the branch ("葡萄东湖路店"):
        # strip the branch words from the customer segment so item parsing
        # does not see them.
        seg0 = segments[0] if segments else ""
        seg0 = seg0.replace(branch, "")
        rest = [s.replace(branch, "") for s in segments[1:]]
        segments = ([seg0] if seg0 else []) + rest
        segments = [s for s in segments if s and _py_full(s) != branch_py]
        cust_i = 0 if seg0 else -1
        if not segments:
            return {"customer": {"name": cust["name"],
                                 "customer_name": cust["customer_name"]},
                    "customer_phrase": _customer_voice_phrase(text),
                    "items": [], "unmatched": [], "notes": None,
                    "shipping_rule": shipping, "freight": freight}
    out = []
    notes = []
    # users sometimes repeat the customer name ("饕餮屈勇，饕餮屈勇，…") —
    # skip echoes of the customer segment so they are not parsed as items
    cust_py = _py_full(segments[cust_i]) if cust_i >= 0 else None
    for idx, seg in enumerate(segments):
        if idx == cust_i:
            continue
        if cust_py and _py_full(seg) == cust_py:
            continue
        # A spoken vintage (小海龙2025年) pins the vintage; strip it so it
        # does not disturb alias/name matching.
        spoken_year = _spoken_vintage(seg)
        if spoken_year:
            seg = _YEAR_NUM_RE.sub("", seg, count=1)
            seg = _YEAR_CN_RE.sub("", seg, count=1)
        name_part, qty = _split_qty(seg)
        alias = _alias_for(name_part)
        if alias:
            # A curated alias may have been created against an older code,
            # so promote it within its product family before exact
            # item-code matching (to the spoken vintage, else the newest).
            latest_alias_item = _latest_vintage_item(alias, items)
            name_part = (latest_alias_item or {}).get("item_code", alias)
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
        if spoken_year:
            vintage_row = _vintage_match_item(best["item_code"],
                                              spoken_year, items)
            if not vintage_row:
                return None  # spoken vintage not in the family -> LLM
            best = vintage_row
        # bare vineyard name tying across different traits (天梯园 -> GG vs
        # 珍藏 vs 晚摘 vs 串选)? Rule: the customer must name the variant.
        # Don't guess, don't burn an LLM call — drop the row and say so.
        tied = [it for it in items
                if it is not best and bs > 0 and
                abs(_relevance(sn, sp, it, "item_name") - bs) <= 0.01]
        traits = {_traits(it.get("item_name")) for it in [best, *tied]}
        if len(traits) > 1:
            labels = "、".join(
                "".join(t.upper() if t in ("gg", "tba") else t
                        for t in sorted(tr))
                for tr in sorted(traits, key=lambda t: sorted(t)))
            notes.append(f"「{seg}」有多款（{labels}），请明说哪一款")
            continue
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
        # customer-only utterance ("北京普道店") — no need for the slow LLM
        # round-trip just to confirm the customer; ambiguous candidates
        # still fall through. Ambiguous-item notes must survive though
        # ("两瓶天梯园" alone: customer + the please-clarify note).
        if cust and cust_score >= 4 and not cust_candidates:
            return {"customer": {"name": cust["name"],
                                 "customer_name": cust["customer_name"]},
                    "customer_phrase": segments[cust_i] if cust_i >= 0
                    else _customer_voice_phrase(text),
                    "items": [], "unmatched": [],
                    "notes": "；".join(notes) or None,
                    "shipping_rule": shipping, "freight": freight}
        if notes:
            # only an ambiguous item was said — answer locally with the
            # clarify note instead of an LLM round-trip
            return {"customer": None, "customer_phrase": None,
                    "items": [], "unmatched": [],
                    "notes": "；".join(notes),
                    "shipping_rule": shipping, "freight": freight}
        return None
    result = {"customer": None if cust_candidates else
              {"name": cust["name"], "customer_name": cust["customer_name"]},
              "customer_phrase": segments[cust_i] if cust_i >= 0
              else _customer_voice_phrase(text),
              "items": out, "unmatched": [],
              "notes": "；".join(notes) or None,
              "shipping_rule": shipping, "freight": freight}
    if cust_candidates:
        result["customer_candidates"] = [
            {"name": c["name"], "customer_name": c["customer_name"]}
            for c in cust_candidates]
    return result


def _normalize_transcript_text(text):
    # whisper mishears 五瓶 as 物品 (wǔpíng/wùpǐn); the app never uses 物品
    text = text.replace("物品", "五瓶")
    # and 瓶 as 平/坪 after a number ("8平" -> "8瓶", "两坪" -> "两瓶")
    text = re.sub(r"([0-9零一二两三四五六七八九十])\s*[平坪]", r"\1瓶", text)
    # Whisper may insert a space after a leading quantity ("12瓶 天梯园").
    # Keep it in the same segment so it cannot attach to the customer name.
    text = re.sub(
        r"([0-9零一二两三四五六七八九十]{1,3}\s*"
        r"(?:瓶|箱|盒|个|支|件|听))\s+(?=\S)", r"\1", text)
    # Stable Whisper homophones observed in synthesized full-order tests.
    # These are wine names in this app, not ordinary prose.
    replacements = [
        (r"日[轨鬼晷][圆元源]", "日晷园"),
        (r"[荷喝赫][曼慢]干[白摆]", "赫曼干白"),
        (r"森林[圆元源]", "森林园"),
        (r"天梯[圆元]", "天梯园"),
        (r"零[西希吸][圆元源]", "灵犀园"),
        (r"[列页念][墨末莫][圆元源]", "涅墨园"),
        (r"([六6])\s*[好號]", r"\1号"),
        # 葡道 branches are always said with the branch (葡道东湖路店);
        # Whisper hears 葡萄、东湖路店 — rejoin so branch matching works.
        (r"葡萄[、，,]?\s*(?=\S{1,8}店)", "葡道"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def _parse_transcript(text):
    """Shared pipeline: transcript text -> customer + items + notes.
    Fast local parse first; LLM (kimi/codex CLI) only for hard cases."""
    text = _normalize_transcript_text(text)
    with _cache_lock:
        all_customers = list(_cache["customers"])
        all_items = list(_cache["items"])

    fast = _fast_parse(text, all_customers, all_items)
    if fast is not None:
        fast["_path"] = "fast"
        return fast

    customers, items = _prefilter_catalog(text, all_customers, all_items)

    with _cache_lock:
        ship_rules = [r["name"] for r in _cache["shipping_rules"]]
    # Mark customers present in recent ERP orders (☆) and sort them first by
    # recency, so the LLM can apply the newest-order tie-break for shared or
    # homophone spoken names (漾叶 has two ERP IDs: 上海漾叶 / DC1M上海漾叶).
    recent_rank = _customer_recent_order_rank()
    ordered = sorted(
        customers,
        key=lambda c: (c["name"] not in recent_rank,
                       recent_rank.get(c["name"], 0)))
    customer_list = "、".join(
        # pinyin annotation is essential for the small local model to map
        # homophone mishearings (陶铁区泳 ~ taotiequyong)
        f"{c['customer_name']}|{(c.get('_voice_pys') or [''])[0]}"
        + ("☆" if c["name"] in recent_rank else "")
        for c in ordered)

    def _item_line(i):
        # short hanzi name (Latin/number noise stripped, capped) + spoken
        # pinyin — the local model's context is small, keep lines compact
        hanzi = re.sub(r"[^一-鿿]", "", i.get("item_name", ""))[:24]
        spoken = _item_spoken_names(i)
        py = _py_full(spoken[0]) if spoken else ""
        return f"{i['item_code']}|{hanzi}|{py}"

    prompt = _PARSE_PROMPT.format(
        customers=customer_list,
        items="；".join(_item_line(i) for i in items),
        learned=_learned_hint(),
        shipping="、".join(ship_rules))
    parsed, llm_backend = _llm_parse(prompt, text)

    # map LLM output back to real records (full catalog, not just prefiltered)
    customer = None
    if parsed.get("customer"):
        wanted = str(parsed["customer"]).replace("☆", "").strip()
        wanted = wanted.split("|")[0].strip()  # model may echo the pinyin
        matches = [c for c in all_customers
                   if c["customer_name"] == wanted or c["name"] == wanted]
        if len(matches) > 1:
            # shared/homophone name -> newest ERP order wins
            matches.sort(key=lambda c: recent_rank.get(
                c["name"], float("inf")))
        customer = matches[0] if matches else None
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
        # Vintage: use the newest cached vintage UNLESS the dictation named
        # one explicitly (小海龙2025年 -> the 2025 code, even if older).
        spoken_year = _spoken_vintage(str(it.get("phrase") or "")) \
            or _spoken_vintage(text)
        if spoken_year:
            row = _vintage_match_item(row["item_code"], spoken_year,
                                      all_items) or row
        else:
            row = _latest_vintage_item(row["item_code"], all_items) or row
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
        "_path": f"llm/{llm_backend}",
    }


# ------------------------------------------------------- voice transcription
# Local faster-whisper (no API key). Model loads once in the background.

_whisper = {"model": None, "lock": threading.Lock()}


def _item_spoken_names(it):
    """The ways an item is SAID: catalogue lead, winery-stripped form,
    vineyard 园 short names (+GG). Shared by _hotwords and the voice eval."""
    name = it.get("item_name") or ""
    names = []
    lead = re.match(r"[一-鿿]{2,12}", name)
    if not lead:
        return names
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
    return names


def _hotwords(focus_names_override=None):
    """Domain vocabulary for whisper: distinctive item short-names (winery
    prefixes stripped, vineyard 园 names extracted), item codes, customer
    names — so 甜心犬/美鸭鸭/天梯园 are heard correctly.
    Whisper keeps the END of a long prompt, so customers go last."""
    codes, names = [], []
    with _cache_lock:
        items = list(_cache["items"])
        customers = list(_cache["customers"])
    for it in items:
        if it.get("item_code"):
            codes.append(it["item_code"])
        names += _item_spoken_names(it)
    names += ["天梯园", "日晷园", "香料园", "森林园", "修士园", "修道院", "修道园",
              "灵犀园", "灵犀园晚摘", "金滴园", "云岭干红", "云岭", "涅墨园",
              "甜心犬", "美鸭鸭", "森林之约", "凯瑟琳", "GG", "熊进",
              "herman干白", "赫曼干白",
              "德邦", "顺丰", "冰袋", "隔热膜", "运费",
              "雷司令", "丹魄", "莫斯卡托", "阿尔巴利诺", "瓶", "箱"]
    cust_names = [name for c in customers if c.get("customer_name")
                  for name in (c.get("_voice_names") or [
                      c.get("_spoken") or
                      _spoken_customer_name(c["customer_name"])])]
    # branch customers (葡道-陆家嘴店 etc.) are said without the dash —
    # guarantee those spoken forms a hotword slot
    for c in cust_names:
        if "-" in c:
            names.append(c.replace("-", "").replace(" ", ""))
    # frequent customers by recent-order count — these deserve hotword slots
    from collections import Counter
    with _cache_lock:
        recent = list(_cache["recent_orders"])
    freq = Counter(o.get("customer") for o in recent if o.get("customer"))
    customer_names = {c.get("name"): c.get("_spoken")
        or _spoken_customer_name(c.get("customer_name") or c.get("name"))
        for c in customers}
    hot_custs = [customer_names.get(c, _spoken_customer_name(c))
                 for c, _ in freq.most_common(30)]
    # whisper only keeps the END of a long prompt (~few hundred chars), so
    # order by importance: codes & cold customers first (may be dropped),
    # item short names, and hot customers + spoken short forms last
    names = list(dict.fromkeys(names))           # dedupe, keep order
    cust_names = list(dict.fromkeys(x for x in cust_names if x))
    hot_custs = list(dict.fromkeys(x for x in hot_custs if x))
    if focus_names_override is None:
        try:
            with open("customer_voice_focus.json") as f:
                focus_names = json.load(f)
        except (OSError, ValueError, TypeError):
            focus_names = []
    else:
        focus_names = focus_names_override
    focus_names = [_spoken_customer_name(x) for x in focus_names if x]
    # Focus names go last because Whisper retains the end of a long prompt.
    return "，".join(codes + cust_names + names + hot_custs
                    + focus_names)[-1600:]


def _whisper_model():
    if _whisper["model"] is None:
        with _whisper["lock"]:
            if _whisper["model"] is None:
                from faster_whisper import WhisperModel
                _whisper["model"] = WhisperModel(
                    "small", device="cpu", compute_type="int8")
    return _whisper["model"]


def _customer_voice_candidates(text, limit=5):
    """Closest spoken customer names for a focused second ASR pass."""
    phrase = _customer_voice_phrase(text) or text
    phrase_n = _norm(phrase)
    phrase_py = _py_full(phrase)
    with _cache_lock:
        customers = list(_cache["customers"])
    scores = {}
    recent_rank = _customer_recent_order_rank()
    for customer in customers:
        voice_names = customer.get("_voice_names") or [
            customer.get("_spoken") or _spoken_customer_name(
                customer.get("customer_name") or customer.get("name"))]
        voice_norms = customer.get("_voice_norms") or [_norm(x) for x in voice_names]
        voice_pys = customer.get("_voice_pys") or [_py_full(x) for x in voice_names]
        for spoken, spoken_n, spoken_py in zip(
                voice_names, voice_norms, voice_pys):
            score = max(
                difflib.SequenceMatcher(None, phrase_n, spoken_n).ratio(),
                difflib.SequenceMatcher(None, phrase_py, spoken_py).ratio())
            key = (spoken, customer["name"])
            scores[key] = max(score, scores.get(key, 0.0))
    ranked = [(score, spoken, customer_id)
              for (spoken, customer_id), score in scores.items()]
    ranked.sort(key=lambda row: (-row[0], recent_rank.get(
        row[2], float("inf"))))
    # Whisper needs names, not duplicate customer IDs. Pinyin-equivalent names
    # deliberately keep the customer used most recently in ERP first.
    names = list(dict.fromkeys(row[1] for row in ranked))[:limit]
    top_score = ranked[0][0] if ranked else 0.0
    top_pinyin = _py_full(ranked[0][1]) if ranked else ""
    next_distinct = next((row[0] for row in ranked[1:]
                          if _py_full(row[1]) != top_pinyin), 0.0)
    margin = top_score - next_distinct
    return names, top_score, margin


def _customer_rows_for_spoken_names(names):
    """Resolve ranked spoken names back to customer picker rows."""
    with _cache_lock:
        customers = list(_cache["customers"])
    rows = []
    recent_rank = _customer_recent_order_rank()
    for spoken in names:
        matches = [customer for customer in customers
                   if spoken in (customer.get("_voice_names") or [])]
        matches.sort(key=lambda customer: recent_rank.get(
            customer["name"], float("inf")))
        for customer in matches:
            if all(row["name"] != customer["name"] for row in rows):
                rows.append({"name": customer["name"],
                             "customer_name": customer["customer_name"]})
    return rows


def _transcribe_audio(path, initial_prompt, hotwords):
    segments, _ = _whisper_model().transcribe(
        path, language="zh", beam_size=1, vad_filter=True,
        hotwords=hotwords, initial_prompt=initial_prompt)
    return "".join(segment.text for segment in segments).strip()


def _recognize_order_audio(path, hotwords=None):
    """Production ASR pipeline, shared by the API and voice evaluator."""
    base_prompt = "葡萄酒销售订单，包含客户名称、商品名称和数量（瓶/箱）。"
    hotwords = _hotwords() if hotwords is None else hotwords
    text = _normalize_transcript_text(
        _transcribe_audio(path, base_prompt, hotwords))
    candidates, score, margin = _customer_voice_candidates(text)
    phrase = _norm(_customer_voice_phrase(text) or text)
    exact = any(phrase == _norm(name) for name in candidates)
    passes = 1
    if text and candidates and not exact and (score < 0.88 or margin < 0.12):
        retry_prompt = (base_prompt + " 候选客户："
                        + "、".join(candidates) + "。")
        retry_text = _normalize_transcript_text(
            _transcribe_audio(path, retry_prompt, hotwords))
        passes += 1
        retry_candidates, retry_score, retry_margin = \
            _customer_voice_candidates(retry_text)
        if retry_text and retry_score > score:
            text = retry_text
            candidates = retry_candidates
            score = retry_score
            margin = retry_margin
    customer_phrase = _customer_voice_phrase(text) or text
    phrase = _norm(customer_phrase)
    exact = any(phrase == _norm(name) for name in candidates)
    uncertain = not exact and (score < 0.88 or margin < 0.12)
    return {"text": text, "customer_phrase": customer_phrase,
            "customer_candidates": candidates, "customer_score": score,
            "customer_margin": margin, "customer_exact": exact,
            "customer_uncertain": uncertain, "passes": passes}


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
        import time
        t0 = time.time()
        recognition = _recognize_order_audio(tmp.name)
        text = recognition["text"]
        t_asr = time.time()
        if not text:
            dest = _save_recording(tmp.name, suffix, "")
            _vlog({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                   "src": "audio", "file": os.path.basename(dest),
                   "error": "empty transcript"})
            return jsonify({"error": "没听清，请再试一次"}), 422
        result = _parse_transcript(text)
        # Keep the best parsed customer selected, but flag weak/close evidence
        # so the UI prominently asks the user to verify it.
        if recognition["customer_uncertain"]:
            picker_rows = _customer_rows_for_spoken_names(
                recognition["customer_candidates"][:5])
            result["customer_uncertain"] = True
            result["customer_suggestions"] = picker_rows
            result["customer_phrase"] = recognition["customer_phrase"]
        result["text"] = text
        dest = _save_recording(tmp.name, suffix, text)
        _vlog({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "src": "audio", "file": os.path.basename(dest), "text": text,
               "path": result.get("_path"),
               "asr_ms": round((t_asr - t0) * 1000),
               "ms": round((time.time() - t0) * 1000),
               "customer": (result.get("customer") or {}).get("customer_name"),
               "items": [[i["item_code"], i["qty"]] for i in result.get("items", [])],
               "shipping": result.get("shipping_rule"),
               "freight": result.get("freight")})
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
                              "status", "docstatus"]),
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
    payload = request.get_json(silent=True) or {}
    r = erp.call("POST",
                 "/api/method/erpnext.selling.doctype.sales_order"
                 ".sales_order.make_delivery_note",
                 data={"source_name": name})
    body, code = erp_json(r)
    if code != 200:
        return jsonify(body), code
    doc = body["message"]
    if payload.get("shipping_address_name"):
        doc["shipping_address_name"] = payload["shipping_address_name"]
    if payload.get("contact_person"):
        contact_name = payload["contact_person"]
        # Mapping the Sales Order has already populated its old dependent
        # contact fields. Resolve the newly selected Customer-linked Contact
        # and explicitly refresh the phone used by the print format.
        contact_r = erp.call("GET", "/api/resource/Contact", params={
            "fields": json.dumps(["name", "mobile_no", "phone"]),
            "filters": json.dumps([
                ["name", "=", contact_name],
                ["Dynamic Link", "link_doctype", "=", "Customer"],
                ["Dynamic Link", "link_name", "=", doc.get("customer")],
            ]),
            "limit_page_length": 1,
        })
        contact_body, contact_code = erp_json(contact_r)
        if contact_code != 200:
            return jsonify(contact_body), contact_code
        contacts = contact_body.get("data", [])
        if not contacts:
            return jsonify({"error": "联系人不属于这个客户"}), 400
        contact = contacts[0]
        doc["contact_person"] = contact["name"]
        doc["contact_mobile"] = contact.get("mobile_no") or \
            contact.get("phone") or ""
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
    if code != 200:
        _log_api_error(f"POST /api/deliveries/{name}/submit",
                       {"docstatus": 1}, code, body)
    return jsonify(body.get("data", body)
                   if code == 200 else _friendly_error(body)), code


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
