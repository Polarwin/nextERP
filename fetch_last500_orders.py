"""Fetch the last 500 ERP Sales Orders (customer + items) into
/tmp/last500_orders.json for the random500 voice-eval suite.

Read-only. The API user may not query the Sales Order Item child doctype
directly (403), so items come from per-document GETs, fetched concurrently
(the ERP is high-latency; 8 workers keep this around a minute).
Same output shape as /tmp/last20_orders.json.
"""
import json
from concurrent.futures import ThreadPoolExecutor

import server

ORDERS_FILE = "/tmp/last500_orders.json"
LIMIT = 500
WORKERS = 8


def fetch_order(name):
    r = server.erp.call("GET", f"/api/resource/Sales Order/{name}")
    body, _ = server.erp_json(r)
    doc = body.get("data", {})
    return [{"item_code": it["item_code"], "qty": it["qty"]}
            for it in doc.get("items", [])]


def main():
    r = server.erp.call("GET", "/api/resource/Sales Order", params={
        "fields": json.dumps(["name", "customer"]),
        "order_by": "creation desc",
        "limit_page_length": LIMIT})
    orders, _ = server.erp_json(r)
    rows = orders["data"]
    print(f"{len(rows)} orders", flush=True)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        item_lists = list(pool.map(lambda o: fetch_order(o["name"]), rows))

    out = [{"name": o["name"], "customer": o["customer"], "items": items}
           for o, items in zip(rows, item_lists)]
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    customers = {o["customer"] for o in out}
    codes = {it["item_code"] for o in out for it in o["items"]}
    print(f"Wrote {ORDERS_FILE}: {len(customers)} distinct customers, "
          f"{len(codes)} distinct items")


if __name__ == "__main__":
    main()
