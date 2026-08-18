"""Train and evaluate a local voice alias map from recent ERP orders.

Read-only toward ERP. Orders are deterministically split 60/20/20 before
training; only training transcripts may add aliases. Validation decides
whether the candidate map is installed, and test is reported untouched.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
import os
import random

import edge_tts

import server


OUT_DIR = "voice_map_experiment"
AUDIO_DIR = os.path.join(OUT_DIR, "audio")
DATASET_FILE = os.path.join(OUT_DIR, "dataset.json")
TRANSCRIPTS_FILE = os.path.join(OUT_DIR, "transcripts.json")
REPORT_FILE = os.path.join(OUT_DIR, "report.json")
CANDIDATE_MAP_FILE = os.path.join(OUT_DIR, "candidate_map.json")
BASE_MAP_FILE = os.path.join(OUT_DIR, "base_map.json")
VOICE = "zh-CN-XiaoxiaoNeural"
SEED = 8347
PATTERNS = [
    ("customer_then_verb_qty_item", "{c}要{q}瓶{i}"),
    ("verb_before_customer_qty_item", "给{c}{q}瓶{i}"),
    ("customer_no_verb_qty_item", "{c}{q}瓶{i}"),
    ("customer_then_verb_item_qty", "{c}要{i}{q}瓶"),
    ("verb_before_customer_item_qty", "给{c}{i}{q}瓶"),
    ("customer_no_verb_item_qty", "{c}，{i}，{q}瓶"),
]


def _json_read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _json_write(path, value):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def _cn_number(value):
    value = int(value)
    digits = "零一二三四五六七八九"
    if value < 10:
        return "两" if value == 2 else digits[value]
    if value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    if value < 100:
        return digits[value // 10] + "十" + (
            digits[value % 10] if value % 10 else "")
    return str(value)


def fetch_latest_orders(limit=200, workers=8):
    params = {
        "fields": json.dumps(["name", "customer"]),
        "filters": json.dumps([["docstatus", "!=", 2]]),
        "order_by": "creation desc",
        "limit_page_length": limit,
    }
    response = server.erp.call(
        "GET", "/api/resource/Sales Order", params=params)
    response.raise_for_status()
    heads = response.json().get("data", [])[:limit]

    def fetch(head):
        r = server.erp.call(
            "GET", "/api/resource/Sales Order/" + head["name"])
        r.raise_for_status()
        doc = r.json().get("data", {})
        return {
            "name": doc.get("name") or head["name"],
            "customer": doc.get("customer") or head["customer"],
            "items": [{"item_code": row.get("item_code"),
                       "qty": row.get("qty")}
                      for row in doc.get("items", [])],
        }

    fetched = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch, head): head["name"] for head in heads}
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            fetched[row["name"]] = row
            if index % 25 == 0 or index == len(heads):
                print(f"ERP {index}/{len(heads)}", flush=True)
    return [fetched[head["name"]] for head in heads
            if head["name"] in fetched]


def _shortest_customer(customer):
    names = customer.get("_voice_names") or [customer.get("_spoken")]
    names = [name for name in names if name]
    return min(names, key=lambda name: (len(name), name)) if names else None


def _item_aliases_by_code():
    aliases = _json_read(server._VOICE_ALIAS_FILE, {})
    with server._cache_lock:
        codes = {row["item_code"] for row in server._cache["items"]}
    result = {}
    for phrase, target in aliases.items():
        if target in codes:
            result.setdefault(target, []).append(phrase)
    return result


def _shortest_item(item, aliases_by_code):
    names = server._item_spoken_names(item) + aliases_by_code.get(
        item["item_code"], [])
    names = list(dict.fromkeys(name for name in names if len(name) >= 2))
    return min(names, key=lambda name: (len(name), name)) if names else None


def build_dataset(orders):
    with server._cache_lock:
        customers = {row["name"]: row for row in server._cache["customers"]}
        items = {row["item_code"]: row for row in server._cache["items"]}
    aliases_by_code = _item_aliases_by_code()
    spoken_to_codes = {}
    for item in items.values():
        for spoken in server._item_spoken_names(item):
            spoken_to_codes.setdefault(spoken, set()).add(item["item_code"])
    for code, aliases in aliases_by_code.items():
        for spoken in aliases:
            spoken_to_codes.setdefault(spoken, set()).add(code)
    usable = []
    for order in orders:
        customer = customers.get(order["customer"])
        customer_spoken = _shortest_customer(customer or {})
        candidates = []
        for row in order.get("items", []):
            item = items.get(row.get("item_code"))
            try:
                qty = int(float(row.get("qty")))
            except (TypeError, ValueError):
                continue
            item_spoken = _shortest_item(item, aliases_by_code) if item else None
            if item_spoken and qty > 0:
                candidates.append((item, item_spoken, qty))
        if not customer_spoken or not candidates:
            continue
        # Rotate through real order lines so frequent multi-item orders do not
        # always contribute only their first item.
        item, item_spoken, qty = candidates[len(usable) % len(candidates)]
        matching_codes = spoken_to_codes.get(item_spoken, {item["item_code"]})
        expected_code = max(matching_codes, key=lambda code: server._vintage(
            items.get(code, {"item_code": code})))
        usable.append({
            "order": order["name"],
            "source_customer_id": order["customer"],
            "customer_id": _expected_customer_id(customer_spoken),
            "customer_spoken": customer_spoken,
            "source_item_code": item["item_code"],
            "item_code": expected_code,
            "item_spoken": item_spoken,
            "quantity": qty,
        })

    rng = random.Random(SEED)
    rng.shuffle(usable)
    train_end = round(len(usable) * 0.60)
    validation_end = train_end + round(len(usable) * 0.20)
    for index, row in enumerate(usable):
        row["split"] = ("train" if index < train_end else
                        "validation" if index < validation_end else "test")
        pattern_name, pattern = PATTERNS[index % len(PATTERNS)]
        row["pattern"] = pattern_name
        row["sentence"] = pattern.format(
            c=row["customer_spoken"], q=_cn_number(row["quantity"]),
            i=row["item_spoken"])
        row["audio"] = _audio_path(row["sentence"])
    return usable


def _expected_customer_id(spoken):
    spoken_py = server._py_full(spoken)
    recent = server._customer_recent_order_rank()
    with server._cache_lock:
        customers = list(server._cache["customers"])
    matches = [row for row in customers
               if spoken_py in (row.get("_voice_pys") or [])]
    matches.sort(key=lambda row: recent.get(row["name"], float("inf")))
    return matches[0]["name"] if matches else None


def _audio_path(sentence):
    digest = hashlib.sha256(sentence.encode()).hexdigest()[:20]
    return os.path.join(AUDIO_DIR, digest + ".mp3")


async def _synthesize_one(row, semaphore):
    path = row["audio"]
    if os.path.exists(path) and os.path.getsize(path) > 100:
        return
    async with semaphore:
        await edge_tts.Communicate(row["sentence"], VOICE).save(path)


async def synthesize(dataset, concurrency=8):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [_synthesize_one(row, semaphore) for row in dataset]
    for index, task in enumerate(asyncio.as_completed(tasks), 1):
        await task
        if index % 25 == 0 or index == len(tasks):
            print(f"TTS {index}/{len(tasks)}", flush=True)


def transcribe(dataset):
    saved = _json_read(TRANSCRIPTS_FILE, {})
    hotwords = server._hotwords()
    for index, row in enumerate(dataset, 1):
        key = row["order"]
        if key not in saved:
            recognition = server._recognize_order_audio(
                row["audio"], hotwords=hotwords)
            saved[key] = {
                "text": recognition["text"],
                "passes": recognition["passes"],
            }
        if index % 10 == 0 or index == len(dataset):
            _json_write(TRANSCRIPTS_FILE, saved)
            print(f"ASR {index}/{len(dataset)}", flush=True)
    return saved


def _parse_row(row, transcript):
    with server._cache_lock:
        customers = list(server._cache["customers"])
        items = list(server._cache["items"])
    parsed = server._fast_parse(transcript, customers, items)
    actual_customer = ((parsed or {}).get("customer") or {}).get("name")
    actual_items = (parsed or {}).get("items") or []
    customer_ok = actual_customer == row["customer_id"]
    item_ok = any(item["item_code"] == row["item_code"] and
                  item["qty"] == row["quantity"] for item in actual_items)
    return {
        "customer_ok": customer_ok,
        "item_ok": item_ok,
        "matched": customer_ok and item_ok,
        "actual_customer": actual_customer,
        "actual_items": [{"item_code": item["item_code"], "qty": item["qty"]}
                         for item in actual_items],
    }


def evaluate(dataset, transcripts):
    results = {}
    for row in dataset:
        result = _parse_row(row, transcripts[row["order"]]["text"])
        results[row["order"]] = {**row, **result,
                                 "heard": transcripts[row["order"]]["text"]}
    return results


def _metrics(results, split):
    rows = [row for row in results.values() if row["split"] == split]
    return {
        "total": len(rows),
        "matched": sum(row["matched"] for row in rows),
        "customer_matched": sum(row["customer_ok"] for row in rows),
        "item_quantity_matched": sum(row["item_ok"] for row in rows),
        "accuracy": round(sum(row["matched"] for row in rows) / len(rows), 4)
                    if rows else 0,
    }


def train_map(base_map, train_results):
    candidate = copy.deepcopy(base_map)
    candidate.setdefault("customers", {})
    candidate.setdefault("items", {})
    additions = []
    with server._cache_lock:
        customers = list(server._cache["customers"])
        items = list(server._cache["items"])
    customer_owners = {}
    for customer in customers:
        for name, pinyin in zip(customer.get("_voice_names", []),
                                customer.get("_voice_pys", [])):
            customer_owners.setdefault(server._norm(name), set()).add(
                customer["name"])
            customer_owners.setdefault(pinyin, set()).add(customer["name"])
    item_owners = {}
    for item in items:
        for name in server._item_spoken_names(item):
            item_owners.setdefault(server._norm(name), set()).add(
                item["item_code"])
            item_owners.setdefault(server._py_full(name), set()).add(
                item["item_code"])
    for row in train_results.values():
        if row["split"] != "train" or row["matched"]:
            continue
        heard = row["heard"]
        if not row["customer_ok"]:
            phrase = server._customer_voice_phrase(heard)
            phrase_norm = server._norm(phrase)
            expected_len = len(server._norm(row["customer_spoken"]))
            # Reject a failed segmentation that swallowed quantity/item text.
            if (phrase and len(phrase_norm) >= 2 and
                    not any(ch.isdigit() for ch in phrase_norm) and
                    len(phrase_norm) <= max(6, expected_len * 2)):
                entry = {"phrase": phrase, "customer": row["customer_id"]}
                keys = {server._norm(phrase), server._py_full(phrase)}
                owners = set().union(*(
                    customer_owners.get(key, set()) for key in keys))
                conflicts = [candidate["customers"].get(key)
                             for key in keys
                             if candidate["customers"].get(key, {}).get(
                                 "customer") not in (None, row["customer_id"])]
                if owners - {row["customer_id"]} or conflicts:
                    continue
                for key in keys:
                    if key:
                        candidate["customers"][key] = entry
                additions.append({"type": "customer", "phrase": phrase,
                                  "target": row["customer_id"]})
        # Single-item training sentences allow conservative item extraction.
        if row["customer_ok"] and not row["item_ok"]:
            segments = server._seg_list(heard)
            phrase = None
            if len(segments) >= 2:
                phrase = server._split_qty(segments[-1])[0].strip()
            if phrase and len(server._norm(phrase)) >= 2:
                entry = {"phrase": phrase, "item_code": row["item_code"]}
                keys = {server._norm(phrase), server._py_full(phrase)}
                owners = set().union(*(
                    item_owners.get(key, set()) for key in keys))
                conflicts = [candidate["items"].get(key)
                             for key in keys
                             if candidate["items"].get(key, {}).get(
                                 "item_code") not in (None, row["item_code"])]
                if owners - {row["item_code"]} or conflicts:
                    continue
                for key in keys:
                    if key:
                        candidate["items"][key] = entry
                additions.append({"type": "item", "phrase": phrase,
                                  "target": row["item_code"]})
    return candidate, additions


def run(args):
    orders = fetch_latest_orders(args.orders, args.erp_workers)
    dataset = build_dataset(orders)
    _json_write(DATASET_FILE, dataset)
    counts = {split: sum(row["split"] == split for row in dataset)
              for split in ("train", "validation", "test")}
    print(f"Dataset {len(dataset)}: {counts}", flush=True)
    asyncio.run(synthesize(dataset, args.tts_concurrency))
    transcripts = transcribe(dataset)

    base_map = server._load_learned()
    _json_write(BASE_MAP_FILE, base_map)
    baseline = evaluate(dataset, transcripts)
    candidate, additions = train_map(base_map, baseline)
    _json_write(CANDIDATE_MAP_FILE, candidate)

    # Temporarily install the candidate for deterministic text replay.
    _json_write(server._LEARN_FILE, candidate)
    trained = evaluate(dataset, transcripts)
    base_metrics = {s: _metrics(baseline, s)
                    for s in ("train", "validation", "test")}
    trained_metrics = {s: _metrics(trained, s)
                       for s in ("train", "validation", "test")}
    install = (trained_metrics["validation"]["accuracy"] >
               base_metrics["validation"]["accuracy"] and
               trained_metrics["test"]["accuracy"] >=
               base_metrics["test"]["accuracy"])
    if not install:
        _json_write(server._LEARN_FILE, base_map)

    report = {
        "seed": SEED,
        "voice": VOICE,
        "orders_requested": args.orders,
        "orders_usable": len(dataset),
        "split_counts": counts,
        "patterns": [name for name, _ in PATTERNS],
        "base_metrics": base_metrics,
        "trained_metrics": trained_metrics,
        "map_entries_before": {k: len(base_map.get(k, {}))
                               for k in ("customers", "items")},
        "map_entries_after": {k: len(candidate.get(k, {}))
                              for k in ("customers", "items")},
        "additions": additions,
        "candidate_installed": install,
        "baseline_results": baseline,
        "trained_results": trained,
    }
    _json_write(REPORT_FILE, report)
    print(json.dumps({
        "base": base_metrics,
        "trained": trained_metrics,
        "additions": len(additions),
        "installed": install,
        "report": REPORT_FILE,
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", type=int, default=200)
    parser.add_argument("--erp-workers", type=int, default=8)
    parser.add_argument("--tts-concurrency", type=int, default=8)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
