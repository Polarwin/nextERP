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
from flask import Flask, jsonify, request, send_from_directory, Response

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
