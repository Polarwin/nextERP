"""Generate and test random voice orders from the latest ERP order pool.

ERP access is read-only. Customer and item pools come exclusively from the
latest N non-cancelled Sales Orders. Audio/transcripts/results are resumable
and kept in a gitignored local directory for future regression tests.
"""
import argparse
import asyncio
import hashlib
import json
import os
import random
import re

import edge_tts

import server
import voice_map_experiment as common


OUT_DIR = "voice_recent_random_test"
AUDIO_DIR = os.path.join(OUT_DIR, "audio")
CASES_FILE = os.path.join(OUT_DIR, "cases.json")
TRANSCRIPTS_FILE = os.path.join(OUT_DIR, "transcripts.json")
RESULTS_FILE = os.path.join(OUT_DIR, "results.json")
VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_SEED = 8347


def _write(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def _audio_path(sentence):
    digest = hashlib.sha256(sentence.encode()).hexdigest()[:20]
    return os.path.join(AUDIO_DIR, digest + ".mp3")


def build_cases(orders, count=100, seed=DEFAULT_SEED):
    with server._cache_lock:
        customers = {row["name"]: row for row in server._cache["customers"]}
        items = {row["item_code"]: row for row in server._cache["items"]}
    aliases_by_code = common._item_aliases_by_code()
    names_by_code = {}
    owners = {}
    for item in items.values():
        names = list(dict.fromkeys(
            server._item_spoken_names(item) +
            aliases_by_code.get(item["item_code"], [])))
        names_by_code[item["item_code"]] = names
        for name in names:
            owners.setdefault(name, set()).add(item["item_code"])

    def family(code):
        match = re.match(r"^(.*)-(\d{2})(H?)$", code, re.I)
        return (match.group(1).lower(), match.group(3).lower()) \
            if match else (code.lower(), "")

    def shortest_unambiguous(item):
        source_family = family(item["item_code"])
        choices = []
        for name in names_by_code.get(item["item_code"], []):
            families = {family(code) for code in owners.get(name, set())}
            if len(name) >= 2 and families == {source_family}:
                choices.append(name)
        if not choices:
            return None, None
        name = min(choices, key=lambda value: (len(value), value))
        newest = max(owners[name], key=lambda code: server._vintage(
            items.get(code, {"item_code": code})))
        return name, newest

    customer_pool = []
    item_pool = []
    for order in orders:
        customer = customers.get(order.get("customer"))
        spoken = common._shortest_customer(customer or {})
        if spoken:
            customer_pool.append((spoken,
                                  common._expected_customer_id(spoken)))
        for line in order.get("items", []):
            item = items.get(line.get("item_code"))
            item_spoken, expected_code = shortest_unambiguous(item) \
                if item else (None, None)
            try:
                quantity = int(float(line.get("qty")))
            except (TypeError, ValueError):
                continue
            if item_spoken and quantity > 0:
                item_pool.append((item_spoken, expected_code, quantity))

    customer_pool = list(dict.fromkeys(customer_pool))
    item_pool = list(dict.fromkeys(item_pool))
    if not customer_pool or not item_pool:
        raise RuntimeError("Recent orders produced an empty customer/item pool")

    rng = random.Random(seed)
    cases, sentences = [], set()
    attempts = 0
    while len(cases) < count and attempts < count * 50:
        attempts += 1
        customer_spoken, customer_id = rng.choice(customer_pool)
        item_spoken, item_code, historical_qty = rng.choice(item_pool)
        # Mostly retain a real recent-order quantity, while occasionally using
        # another common recent quantity to exercise placement independently.
        quantity = historical_qty if rng.random() < 0.7 else rng.choice(
            [row[2] for row in item_pool])
        pattern_name, pattern = rng.choice(common.PATTERNS)
        sentence = pattern.format(
            c=customer_spoken, q=common._cn_number(quantity), i=item_spoken)
        if sentence in sentences:
            continue
        sentences.add(sentence)
        cases.append({
            "case": len(cases) + 1,
            "pattern": pattern_name,
            "sentence": sentence,
            "customer_spoken": customer_spoken,
            "expected_customer_id": customer_id,
            "item_spoken": item_spoken,
            "expected_item_code": item_code,
            "quantity": quantity,
            "audio": _audio_path(sentence),
        })
    if len(cases) < count:
        raise RuntimeError(f"Could only construct {len(cases)}/{count} cases")
    return cases, len(customer_pool), len(item_pool)


async def _synthesize_one(case, semaphore):
    path = case["audio"]
    if os.path.exists(path) and os.path.getsize(path) > 100:
        return
    async with semaphore:
        await edge_tts.Communicate(case["sentence"], VOICE).save(path)


async def synthesize(cases, concurrency):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [_synthesize_one(case, semaphore) for case in cases]
    for index, task in enumerate(asyncio.as_completed(tasks), 1):
        await task
        if index % 20 == 0 or index == len(tasks):
            print(f"TTS {index}/{len(tasks)}", flush=True)


def transcribe(cases, fresh=False):
    saved = {} if fresh else _read(TRANSCRIPTS_FILE, {})
    hotwords = server._hotwords()
    for index, case in enumerate(cases, 1):
        key = str(case["case"])
        if key not in saved or saved[key].get("sentence") != case["sentence"]:
            recognition = server._recognize_order_audio(
                case["audio"], hotwords=hotwords)
            saved[key] = {
                "sentence": case["sentence"],
                "text": recognition["text"],
                "passes": recognition["passes"],
                "uncertain": recognition["customer_uncertain"],
            }
        if index % 10 == 0 or index == len(cases):
            _write(TRANSCRIPTS_FILE, saved)
            print(f"ASR {index}/{len(cases)}", flush=True)
    return saved


def evaluate(cases, transcripts):
    with server._cache_lock:
        customers = list(server._cache["customers"])
        items = list(server._cache["items"])
    results = []
    for case in cases:
        transcript = transcripts[str(case["case"])]
        parsed = server._fast_parse(transcript["text"], customers, items)
        actual_customer = ((parsed or {}).get("customer") or {}).get("name")
        actual_items = (parsed or {}).get("items") or []
        customer_ok = actual_customer == case["expected_customer_id"]
        item_ok = any(row["item_code"] == case["expected_item_code"] and
                      row["qty"] == case["quantity"] for row in actual_items)
        results.append({
            **case,
            "heard": transcript["text"],
            "passes": transcript["passes"],
            "actual_customer_id": actual_customer,
            "actual_items": [{"item_code": row["item_code"], "qty": row["qty"]}
                             for row in actual_items],
            "customer_ok": customer_ok,
            "item_ok": item_ok,
            "matched": customer_ok and item_ok,
        })
    return results


def report(results, customer_pool_size, item_pool_size, seed):
    by_pattern = {}
    for name, _ in common.PATTERNS:
        rows = [row for row in results if row["pattern"] == name]
        by_pattern[name] = {
            "total": len(rows),
            "matched": sum(row["matched"] for row in rows),
            "accuracy": round(sum(row["matched"] for row in rows) / len(rows), 4)
                        if rows else 0,
        }
    payload = {
        "seed": seed,
        "voice": VOICE,
        "total": len(results),
        "customer_pool_size": customer_pool_size,
        "item_pool_size": item_pool_size,
        "matched": sum(row["matched"] for row in results),
        "customer_matched": sum(row["customer_ok"] for row in results),
        "item_quantity_matched": sum(row["item_ok"] for row in results),
        "accuracy": round(sum(row["matched"] for row in results) /
                          len(results), 4),
        "by_pattern": by_pattern,
        "failures": [row for row in results if not row["matched"]],
        "results": results,
    }
    _write(RESULTS_FILE, payload)
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orders", type=int, default=100)
    parser.add_argument("--clips", type=int, default=100)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--erp-workers", type=int, default=8)
    parser.add_argument("--tts-concurrency", type=int, default=8)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--tts-only", action="store_true")
    args = parser.parse_args()
    orders = common.fetch_latest_orders(args.orders, args.erp_workers)
    cases, customer_count, item_count = build_cases(
        orders, args.clips, args.seed)
    _write(CASES_FILE, cases)
    print(f"Pools: {customer_count} customers, {item_count} item/qty rows")
    asyncio.run(synthesize(cases, args.tts_concurrency))
    if args.tts_only:
        return
    transcripts = transcribe(cases, args.fresh)
    payload = report(evaluate(cases, transcripts), customer_count,
                     item_count, args.seed)
    print(json.dumps({key: payload[key] for key in (
        "total", "matched", "customer_matched",
        "item_quantity_matched", "accuracy", "by_pattern")},
        ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
