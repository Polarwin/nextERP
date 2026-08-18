"""Synthesize full spoken orders and test the production voice pipeline.

The customer dictionary, generated audio, and results stay local/gitignored.
This script only reads ERP cache data and never creates or changes ERP records.
"""
import argparse
import asyncio
import hashlib
import json
import os

import edge_tts

import server


OUT_DIR = "voice_customer_eval"
AUDIO_DIR = os.path.join(OUT_DIR, "order_audio")
RESULTS_FILE = os.path.join(OUT_DIR, "order_results.json")
RANDOM_RESULTS_FILE = os.path.join(OUT_DIR, "random_results.json")
RECENT_RESULTS_FILE = os.path.join(OUT_DIR, "recent_results.json")
RANDOM500_RESULTS_FILE = os.path.join(OUT_DIR, "random500_results.json")
REAL_RESULTS_FILE = os.path.join(OUT_DIR, "real_results.json")
FOCUS_FILE = "customer_voice_focus.json"
# Random suite writes here so concurrent suite runs never clobber each other.
ACTIVE_RESULTS_FILE = RESULTS_FILE
VOICE = "zh-CN-XiaoxiaoNeural"
# Synthetic orders are generated with a rotating set of TTS voices, not just
# one — aliases harvested from a single synthetic voice risk being
# voice-specific. Deterministic per sentence, so re-runs reuse cached audio.
VOICES = ["zh-CN-XiaoxiaoNeural",   # female, warm
          "zh-CN-YunjianNeural",    # male
          "zh-CN-XiaoyiNeural",     # female
          "zh-CN-YunyangNeural"]    # male, news style
QUANTITIES = [(2, "两"), (3, "三"), (6, "六"), (12, "十二")]
WINES = [
    ("日晷园", "GH006-24"),
    ("赫曼干白", "GH001-25"),
    ("森林园GG", "GH003-24"),
    ("天梯园六号", "GH005-23"),
    ("灵犀园晚摘", "GH015-24"),
    ("涅墨园", "ES023-21"),
]
# Fixed anchor customer for item-suite sentences: reliably recognized
# (curated alias + hotword), so failures isolate the item name.
ITEM_SUITE_CUSTOMER = ("熊进", "VIP熊进")

QTY_WORDS = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六",
             7: "七", 8: "八", 9: "九", 10: "十", 12: "十二",
             20: "二十", 24: "二十四"}
# General sentence shapes people actually dictate. {c}=customer,
# {q}=quantity word, {i}=item. 要 is optional; quantity and item may
# come in either order.
PATTERNS = [
    "{c}要{q}瓶{i}",      # 漾叶要三瓶日晷园
    "{c}{q}瓶{i}",        # 漾叶三瓶日晷园
    "{c}要{i}{q}瓶",      # 漾叶要日晷园三瓶
    "{c}{i}{q}瓶",        # 漾叶日晷园三瓶
    "给{c}来{q}瓶{i}",    # 给漾叶来三瓶日晷园
    "{c}，{i}，{q}瓶",    # paused dictation
]


def case_voice(sentence):
    """Deterministic voice rotation by sentence hash (stable across runs)."""
    h = int(hashlib.sha256(sentence.encode()).hexdigest(), 16)
    return VOICES[h % len(VOICES)]


def audio_path(sentence, voice=VOICE):
    # default voice keeps the legacy hash so existing cached audio stays valid
    key = sentence if voice == VOICE else f"{voice}|{sentence}"
    digest = hashlib.sha256(key.encode()).hexdigest()[:20]
    return os.path.join(AUDIO_DIR, f"{digest}.mp3")


def dictionary_rows():
    aliases = server._load_customer_spoken_names()
    return [(customer_id, spoken)
            for customer_id, names in aliases.items()
            for spoken in names]


def expected_customer_id(spoken):
    """Exact-pinyin collisions resolve to the most recently used ERP ID."""
    spoken_py = server._py_full(spoken)
    recent_rank = server._customer_recent_order_rank()
    with server._cache_lock:
        customers = list(server._cache["customers"])
    matches = [row for row in customers
               if spoken_py in (row.get("_voice_pys") or [])]
    if not matches:
        return None
    matches.sort(key=lambda row: recent_rank.get(
        row["name"], float("inf")))
    return matches[0]["name"]


def test_cases(limit=None):
    cases = []
    for index, (dictionary_id, spoken) in enumerate(dictionary_rows()):
        qty, qty_spoken = QUANTITIES[index % len(QUANTITIES)]
        wine_spoken, item_code = WINES[index % len(WINES)]
        sentence = f"{spoken}要{qty_spoken}瓶{wine_spoken}"
        cases.append({
            "suite": "customers",
            "dictionary_customer_id": dictionary_id,
            "expected_customer_id": expected_customer_id(spoken),
            "spoken_name": spoken,
            "wine_name": wine_spoken,
            "expected_item_code": item_code,
            "quantity": qty,
            "sentence": sentence,
        })
    return cases[:limit] if limit else cases


def item_spoken_names():
    """Spoken item name -> item codes that share it (vintage variants)."""
    with server._cache_lock:
        items = list(server._cache["items"])
    names = {}
    for it in items:
        for spoken in server._item_spoken_names(it):
            names.setdefault(spoken, set()).add(it.get("item_code"))
    return {spoken: sorted(codes) for spoken, codes in names.items()}


def item_alias_rows():
    """Spoken alias -> item_code, from the curated voice_aliases.json.
    Only aliases whose target is a real cached item code (some values are
    customer names or trait words like GG)."""
    try:
        with open(server._VOICE_ALIAS_FILE, encoding="utf-8") as f:
            aliases = json.load(f)
    except (OSError, ValueError):
        return []
    with server._cache_lock:
        codes = {it.get("item_code") for it in server._cache["items"]}
    return [(spoken, code) for spoken, code in aliases.items()
            if code in codes]


def _shortest(names):
    return min(names, key=len) if names else None


def recent_test_cases(orders_file="/tmp/last20_orders.json"):
    """Replay the last N real ERP orders the way the user would dictate
    them: shortest spoken customer name + shortest spoken item name +
    the real quantities. Multi-item orders become multi-clause sentences."""
    with open(orders_file, encoding="utf-8") as f:
        orders = json.load(f)
    with server._cache_lock:
        customers = {c["name"]: c for c in server._cache["customers"]}
        items = {it["item_code"]: it for it in server._cache["items"]}
    spoken_to_codes = item_spoken_names()
    alias_by_code = {}
    for spoken, code in item_alias_rows():
        alias_by_code.setdefault(code, []).append(spoken)

    cases = []
    for order in orders:
        cust = customers.get(order["customer"])
        if not cust:
            continue
        cust_spoken = _shortest(cust.get("_voice_names") or
                                [cust.get("_spoken")])
        if not cust_spoken:
            continue
        clauses, expected = [], []
        skip = False
        for row in order["items"]:
            code = row["item_code"]
            it = items.get(code)
            if not it:
                skip = True
                break
            names = (server._item_spoken_names(it) +
                     alias_by_code.get(code, []))
            item_spoken = _shortest(names)
            if not item_spoken:
                skip = True
                break
            qty = int(row["qty"])
            clauses.append(f"{qty}瓶{item_spoken}")
            # When vintages share a spoken name, production must choose newest.
            shared = spoken_to_codes.get(item_spoken, [code])
            newest = max(shared, key=lambda candidate: server._vintage(
                items.get(candidate, {"item_code": candidate})))
            expected.append({"codes": [newest], "qty": qty})
        if skip or not clauses:
            continue
        sentence = f"{cust_spoken}要" + "，".join(clauses)
        cases.append({
            "suite": "recent",
            "order": order["name"],
            "dictionary_customer_id": order["customer"],
            "expected_customer_id": expected_customer_id(cust_spoken),
            "spoken_name": cust_spoken,
            "wine_name": "+".join(e["codes"][0] for e in expected),
            "expected_item_code": expected[0]["codes"][0],
            "expected_items": expected,
            "quantity": expected[0]["qty"],
            "sentence": sentence,
        })
    return cases


def random_test_cases(count=100, seed=None):
    """Random full orders: dictionary customer spoken name x curated item
    alias x random quantity — closer to how orders are actually dictated
    than the systematic suites."""
    import random
    rng = random.Random(seed)
    customers = dictionary_rows()
    aliases = item_alias_rows()
    cases = []
    for _ in range(count):
        dictionary_id, spoken = rng.choice(customers)
        alias_spoken, item_code = rng.choice(aliases)
        qty, qty_spoken = rng.choice(list(QTY_WORDS.items()))
        pattern = rng.choice(PATTERNS)
        sentence = pattern.format(c=spoken, q=qty_spoken, i=alias_spoken)
        cases.append({
            "suite": "random",
            "pattern": pattern,
            "dictionary_customer_id": dictionary_id,
            "expected_customer_id": expected_customer_id(spoken),
            "spoken_name": spoken,
            "wine_name": alias_spoken,
            "expected_item_code": item_code,
            "quantity": qty,
            "sentence": sentence,
        })
    return cases


def random500_pool(orders_file="/tmp/last500_orders.json"):
    """Customers and items of the last N real ERP orders, each reduced to
    its shortest spoken name. Also reports pool members that have no spoken
    name at all (dictionary gaps worth fixing by hand)."""
    with open(orders_file, encoding="utf-8") as f:
        orders = json.load(f)
    with server._cache_lock:
        customers = {c["name"]: c for c in server._cache["customers"]}
        items = {it["item_code"]: it for it in server._cache["items"]}
    alias_by_code = {}
    for spoken, code in item_alias_rows():
        alias_by_code.setdefault(code, []).append(spoken)

    cust_pool, item_pool, gaps_c, gaps_i = [], [], [], []
    for cid in dict.fromkeys(o["customer"] for o in orders):
        cust = customers.get(cid)
        names = [n for n in ((cust or {}).get("_voice_names") or []) if n]
        if not names and cust and cust.get("_spoken"):
            names = [cust["_spoken"]]
        if names:
            cust_pool.append((cid, _shortest(names)))
        else:
            gaps_c.append(cid)
    for code in dict.fromkeys(it["item_code"] for o in orders
                              for it in o["items"]):
        it = items.get(code)
        names = (server._item_spoken_names(it) +
                 alias_by_code.get(code, [])) if it else []
        if names:
            item_pool.append((code, _shortest(names)))
        else:
            gaps_i.append(code)
    print(f"Pool: {len(cust_pool)} customers, {len(item_pool)} items")
    if gaps_c:
        print(f"GAP customers without spoken name ({len(gaps_c)}): "
              + ", ".join(gaps_c))
    if gaps_i:
        print(f"GAP items without spoken name ({len(gaps_i)}): "
              + ", ".join(gaps_i))
    return cust_pool, item_pool


def random500_test_cases(count=100, seed=None,
                         orders_file="/tmp/last500_orders.json"):
    """Random orders over the last-500-orders vocabulary only, shortest
    spoken names — the random suite, focused on what is actually sold."""
    import random
    rng = random.Random(seed)
    cust_pool, item_pool = random500_pool(orders_file)
    spoken_to_codes = item_spoken_names()
    with server._cache_lock:
        items = {it["item_code"]: it for it in server._cache["items"]}
    cases = []
    for _ in range(count):
        cid, spoken = rng.choice(cust_pool)
        code, item_spoken = rng.choice(item_pool)
        qty, qty_spoken = rng.choice(list(QTY_WORDS.items()))
        pattern = rng.choice(PATTERNS)
        # 25% of dictations name the vintage explicitly (小海龙2025年):
        # the spoken vintage then pins the exact code instead of newest.
        expected_codes = None
        if rng.random() < 0.25:
            year = server._vintage(items.get(code, {"item_code": code}))
            if year:
                item_spoken = f"{item_spoken}{year}年"
                expected_codes = [code]
        sentence = pattern.format(c=spoken, q=qty_spoken, i=item_spoken)
        # Vintages sharing this spoken name resolve to the newest vintage.
        if expected_codes is None:
            shared = set(spoken_to_codes.get(item_spoken, [])) | {code}
            newest = max(shared, key=lambda c: server._vintage(
                items.get(c, {"item_code": c})))
            expected_codes = [newest]
        cases.append({
            "suite": "random500",
            "pattern": pattern,
            "dictionary_customer_id": cid,
            "expected_customer_id": expected_customer_id(spoken),
            "spoken_name": spoken,
            "wine_name": item_spoken,
            "expected_item_code": code,
            "expected_item_codes": expected_codes,
            "quantity": qty,
            "sentence": sentence,
        })
    return cases


def item_test_cases(limit=None):
    """One full-order sentence per unique spoken item name.

    The anchor customer is fixed, so a failure isolates the item name.
    Several vintages can share a spoken name; any of them is correct.
    """
    anchor_spoken, anchor_id = ITEM_SUITE_CUSTOMER
    with server._cache_lock:
        items_by_code = {it["item_code"]: it
                         for it in server._cache["items"]}
    cases = []
    for index, (spoken, codes) in enumerate(sorted(item_spoken_names().items())):
        qty, qty_spoken = QUANTITIES[index % len(QUANTITIES)]
        sentence = f"{anchor_spoken}要{qty_spoken}瓶{spoken}"
        cases.append({
            "suite": "items",
            "dictionary_customer_id": anchor_id,
            "expected_customer_id": anchor_id,
            "spoken_name": spoken,
            "wine_name": spoken,
            "expected_item_code": codes[0],
            "expected_item_codes": [max(
                codes, key=lambda code: server._vintage(
                    items_by_code.get(code, {"item_code": code})))],
            "quantity": qty,
            "sentence": sentence,
        })
    return cases[:limit] if limit else cases


async def synthesize_one(case, semaphore):
    voice = case_voice(case["sentence"])
    path = audio_path(case["sentence"], voice)
    if os.path.exists(path) and os.path.getsize(path) > 100:
        return
    async with semaphore:
        await edge_tts.Communicate(case["sentence"], voice).save(path)


async def synthesize_all(cases, concurrency):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [synthesize_one(case, semaphore) for case in cases]
    for index, task in enumerate(asyncio.as_completed(tasks), 1):
        await task
        if index % 25 == 0 or index == len(tasks):
            print(f"TTS {index}/{len(tasks)}", flush=True)


def evaluate(cases, previous=None):
    results = dict(previous or {})
    hotwords = server._hotwords()
    with server._cache_lock:
        customers = list(server._cache["customers"])
        items = list(server._cache["items"])
    for index, case in enumerate(cases, 1):
        key = hashlib.sha256(case["sentence"].encode()).hexdigest()[:20]
        if key in results:
            continue
        voice = case_voice(case["sentence"])
        recognition = server._recognize_order_audio(
            audio_path(case["sentence"], voice), hotwords=hotwords)
        parsed = server._fast_parse(recognition["text"], customers, items)
        actual_customer = ((parsed or {}).get("customer") or {}).get("name")
        parsed_items = (parsed or {}).get("items") or []
        if case.get("expected_items"):
            # multi-item order: every expected clause must be present
            item_match = all(any(
                row["item_code"] in exp["codes"] and row["qty"] == exp["qty"]
                for row in parsed_items) for exp in case["expected_items"])
        else:
            acceptable = case.get("expected_item_codes") or \
                [case["expected_item_code"]]
            item_match = any(
                row["item_code"] in acceptable
                and row["qty"] == case["quantity"] for row in parsed_items)
        customer_match = actual_customer == case["expected_customer_id"]
        results[key] = {
            **case,
            "voice": voice,
            "heard": recognition["text"],
            "actual_customer_id": actual_customer,
            "actual_items": [{"item_code": row["item_code"],
                              "qty": row["qty"]} for row in parsed_items],
            "customer_match": customer_match,
            "item_match": item_match,
            "matched": customer_match and item_match,
            "uncertain": recognition["customer_uncertain"],
            "passes": recognition["passes"],
        }
        if index % 10 == 0 or index == len(cases):
            save_results(results)
            print(f"ASR {index}/{len(cases)}", flush=True)
    return results


def real_cases(log_file="voice_log.jsonl"):
    """Real user dictations from voice_log.jsonl + recordings/ — the ground
    truth TTS only approximates. The logged production result is NOT trusted
    as truth (the user may have corrected it afterwards); diffs between the
    logged and the current pipeline output are flagged for manual review."""
    cases = []
    with open(log_file, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("src") != "audio" or not row.get("text"):
                continue
            path = os.path.join("recordings", row.get("file") or "")
            if os.path.exists(path):
                cases.append({"suite": "real", "ts": row.get("ts"),
                              "audio": path, "sentence": row["text"],
                              "logged_path": row.get("path"),
                              "logged_customer": row.get("customer"),
                              "logged_items": row.get("items")})
    return cases


def evaluate_real(cases):
    hotwords = server._hotwords()
    with server._cache_lock:
        customers = list(server._cache["customers"])
        items = list(server._cache["items"])
    results = {}
    for index, case in enumerate(cases, 1):
        recognition = server._recognize_order_audio(case["audio"],
                                                    hotwords=hotwords)
        parsed = server._fast_parse(recognition["text"], customers, items)
        now_customer = ((parsed or {}).get("customer") or {}).get("name")
        now_items = [[r["item_code"], float(r["qty"])]
                     for r in (parsed or {}).get("items") or []]
        logged_items = [[c, float(q)] for c, q in (case["logged_items"] or [])]
        same = (now_customer == case["logged_customer"]
                and sorted(map(str, now_items)) == sorted(map(str, logged_items)))
        results[case["ts"]] = {
            **case,
            "heard_now": recognition["text"],
            "now_customer_id": now_customer,
            "now_items": now_items,
            "now_path": "fast" if parsed else "llm",
            "same_as_logged": same,
        }
        print(f"ASR {index}/{len(cases)}", flush=True)
    changed = [r for r in results.values() if not r["same_as_logged"]]
    payload = {"total": len(results), "same": len(results) - len(changed),
               "changed_for_review": changed, "results": results}
    with open(REAL_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"Same as logged: {len(results) - len(changed)}/{len(results)}; "
          f"{len(changed)} changed — review {REAL_RESULTS_FILE}")
    for r in changed:
        print(f"  {r['ts']} said={r['sentence']!r}")
        print(f"    logged: {r['logged_customer']} {r['logged_items']}")
        print(f"    now:    {r['now_customer_id']} {r['now_items']} "
              f"({r['now_path']}) heard={r['heard_now']!r}")
    return results


def save_results(results):
    os.makedirs(OUT_DIR, exist_ok=True)
    failures = [row for row in results.values() if not row["matched"]]
    payload = {
        "voice": VOICE,
        "total": len(results),
        "matched": len(results) - len(failures),
        "failure_count": len(failures),
        "failures": failures,
        "results": results,
    }
    with open(ACTIVE_RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    # Keep partial runs useful: the next resumable invocation immediately
    # focuses Whisper on names that have failed so far. The focus file is
    # customer-only vocabulary — item failures are not written there.
    with open(FOCUS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(dict.fromkeys(
            row["spoken_name"] for row in failures
            if row.get("suite", "customers") != "items")),
                  f, ensure_ascii=False, indent=2)


def load_previous():
    try:
        with open(ACTIVE_RESULTS_FILE, encoding="utf-8") as f:
            return json.load(f).get("results", {})
    except (OSError, ValueError, TypeError):
        return {}


def harvest_aliases():
    """Learn aliases from eval failures into learned_aliases.json.

    For each failed case, map what Whisper actually HEARD (customer phrase or
    item name) to the expected target. Unsafe entries are skipped:
    - the heard phrase is itself another customer's/item's spoken name
      (aliasing it would hijack the real name, e.g. homophone 三年间/叁年间)
    - generic terms, or phrases shorter than 2 characters
    - item cases where the right item was found but the qty was wrong
      (not a naming problem)
    """
    previous = load_previous()
    failures = [row for row in previous.values() if not row["matched"]]
    with server._cache_lock:
        customers = list(server._cache["customers"])
        items = list(server._cache["items"])
    # spoken names of OTHER targets — never alias these
    cust_voice = {}   # norm/pinyin -> set of customer ids
    for c in customers:
        for name, py in zip(c.get("_voice_names", []),
                            c.get("_voice_pys", [])):
            cust_voice.setdefault(server._norm(name), set()).add(c["name"])
            cust_voice.setdefault(py, set()).add(c["name"])
    item_voice = {}
    for it in items:
        for spoken in server._item_spoken_names(it):
            item_voice.setdefault(server._norm(spoken), set()).add(
                it.get("item_code"))
            item_voice.setdefault(server._py_full(spoken), set()).add(
                it.get("item_code"))

    learned = server._load_learned()
    learned.setdefault("items", {})
    learned.setdefault("customers", {})
    added, skipped = [], []
    # phrase -> possible targets; one mishearing can map to several customers
    # (葡萄 for each 葡道 branch) — resolve by the newest-order rule below
    cust_proposals = {}
    item_proposals = {}
    for row in failures:
        heard = row.get("heard") or ""
        if not heard:
            continue
        # customer phrase
        if not row["customer_match"] and row.get("expected_customer_id"):
            phrase = server._customer_voice_phrase(heard)
            target = row["expected_customer_id"]
            if phrase and phrase != row.get("spoken_name"):
                if len(phrase) < 2:
                    skipped.append((phrase, target, "too short"))
                else:
                    cust_proposals.setdefault(phrase, set()).add(target)
        # item phrase (only when the right item was not heard at all)
        elif row["customer_match"] and not row["item_match"]:
            acceptable = set(row.get("expected_item_codes")
                             or [row["expected_item_code"]])
            found = {r["item_code"] for r in row.get("actual_items", [])}
            if found & acceptable:
                skipped.append((row["spoken_name"], sorted(acceptable),
                                "qty problem, not naming"))
                continue
            segments = server._seg_list(heard)
            cust_phrase = server._customer_voice_phrase(heard)
            parts = [server._split_qty(s)[0] for s in segments
                     if not s.startswith(cust_phrase[:2])]
            parts = [p for p in parts if p]
            if len(parts) != 1:
                skipped.append((row["spoken_name"], sorted(acceptable),
                                f"cannot isolate item phrase in {heard!r}"))
                continue
            phrase = parts[0]
            # aim the alias at the newest vintage (user rule: always newest)
            target = max(acceptable, key=lambda code: server._vintage(
                {"item_code": code}))
            if phrase == row["spoken_name"] or len(phrase) < 2:
                skipped.append((phrase, target, "name heard correctly"))
                continue
            item_proposals.setdefault(phrase, set()).add(target)
    # Resolve proposals into aliases. User rules:
    # - a phrase shared by several customers -> the one with the newest order
    # - a phrase shared by several vintages  -> the newest vintage
    # - a phrase that IS another target's spoken name -> never alias it
    recent_rank = server._customer_recent_order_rank()
    for phrase, targets in cust_proposals.items():
        target = min(targets, key=lambda c: recent_rank.get(c, float("inf")))
        keys = {server._norm(phrase), server._py_full(phrase)}
        owners = set().union(*(cust_voice.get(k, set()) for k in keys))
        if owners - {target}:
            skipped.append((phrase, target,
                            f"collides with {sorted(owners - {target})}"))
            continue
        for k in keys:
            learned["customers"][k] = {"phrase": phrase, "customer": target}
        added.append((phrase, target))
    for phrase, targets in item_proposals.items():
        target = max(targets,
                     key=lambda code: server._vintage({"item_code": code}))
        keys = {server._norm(phrase), server._py_full(phrase)}
        owners = set().union(*(item_voice.get(k, set()) for k in keys))
        if owners - {target}:
            skipped.append((phrase, target,
                            f"collides with {sorted(owners - {target})}"))
            continue
        for k in keys:
            learned["items"][k] = {"phrase": phrase, "item_code": target}
        added.append((phrase, target))
    with open(server._LEARN_FILE, "w", encoding="utf-8") as f:
        json.dump(learned, f, ensure_ascii=False, indent=1)
    print(f"Learned {len(added)} aliases -> {server._LEARN_FILE}")
    for phrase, target in added:
        print(f"  + {phrase!r} -> {target}")
    print(f"Skipped {len(skipped)} (unsafe):")
    for phrase, target, why in skipped:
        print(f"  - {phrase!r} -> {target}: {why}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["customers", "items", "random",
                                            "recent", "random500", "real",
                                            "all"],
                        default="all")
    parser.add_argument("--random-count", type=int, default=100)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--orders-file")
    parser.add_argument("--tts-concurrency", type=int, default=8)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--tts-only", action="store_true")
    parser.add_argument("--harvest", action="store_true",
                        help="learn aliases from the last run's failures "
                             "into learned_aliases.json, then exit")
    args = parser.parse_args()
    global ACTIVE_RESULTS_FILE
    if args.suite == "random":
        ACTIVE_RESULTS_FILE = RANDOM_RESULTS_FILE
    elif args.suite == "recent":
        ACTIVE_RESULTS_FILE = RECENT_RESULTS_FILE
    elif args.suite == "random500":
        ACTIVE_RESULTS_FILE = RANDOM500_RESULTS_FILE
    if args.harvest:
        harvest_aliases()
        return
    if args.orders_file is None:
        args.orders_file = ("/tmp/last500_orders.json"
                            if args.suite == "random500"
                            else "/tmp/last20_orders.json")
    cases = []
    if args.suite in ("customers", "all"):
        cases += test_cases(args.limit)
    if args.suite in ("items", "all"):
        cases += item_test_cases(args.limit)
    if args.suite == "random":
        cases += random_test_cases(args.random_count, args.seed)
    if args.suite == "recent":
        cases += recent_test_cases(args.orders_file)
    if args.suite == "random500":
        cases += random500_test_cases(args.random_count, args.seed,
                                      args.orders_file)
    if args.suite == "real":
        cases = real_cases()
        print(f"Replaying {len(cases)} real user dictations", flush=True)
        evaluate_real(cases)
        return
    print(f"Testing {len(cases)} full spoken orders", flush=True)
    asyncio.run(synthesize_all(cases, args.tts_concurrency))
    if args.tts_only:
        return
    results = evaluate(cases, {} if args.fresh else load_previous())
    failures = [row for row in results.values() if not row["matched"]]
    save_results(results)
    print(f"Matched: {len(results) - len(failures)}/{len(results)}", flush=True)
    print(f"Results: {ACTIVE_RESULTS_FILE}", flush=True)


if __name__ == "__main__":
    main()
