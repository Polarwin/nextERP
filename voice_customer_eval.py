"""Synthesize and test every customer name against production Whisper.

Generated audio, results, and the local focus file are gitignored. This script
never writes to ERPNext.
"""
import argparse
import asyncio
import hashlib
import json
import os
import re

import edge_tts

import server


OUT_DIR = "voice_customer_eval"
AUDIO_DIR = os.path.join(OUT_DIR, "audio")
RESULTS_FILE = os.path.join(OUT_DIR, "results.json")
FOCUS_FILE = "customer_voice_focus.json"
VOICE = "zh-CN-XiaoxiaoNeural"


def normalized(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).lower())


def audio_path(name):
    digest = hashlib.sha256(name.encode()).hexdigest()[:16]
    return os.path.join(AUDIO_DIR, f"{digest}.mp3")


def customer_names():
    with open("cache/customers.json") as f:
        rows = json.load(f)
    return list(dict.fromkeys(
        server._spoken_customer_name(row.get("customer_name"))
        for row in rows if row.get("customer_name")))


async def synthesize_one(name, semaphore):
    path = audio_path(name)
    if os.path.exists(path) and os.path.getsize(path) > 100:
        return
    async with semaphore:
        await edge_tts.Communicate(name, VOICE).save(path)


async def synthesize_all(names, concurrency):
    os.makedirs(AUDIO_DIR, exist_ok=True)
    semaphore = asyncio.Semaphore(concurrency)
    tasks = [synthesize_one(name, semaphore) for name in names]
    for index, task in enumerate(asyncio.as_completed(tasks), 1):
        await task
        if index % 25 == 0 or index == len(tasks):
            print(f"TTS {index}/{len(tasks)}", flush=True)


def transcribe_all(names, hotwords, label, previous=None):
    results = dict(previous or {})
    model = server._whisper_model()
    for index, name in enumerate(names, 1):
        if name in results:
            continue
        segments, _ = model.transcribe(
            audio_path(name), language="zh", beam_size=1, vad_filter=True,
            hotwords=hotwords,
            initial_prompt="葡萄酒销售订单，包含客户名称、商品名称和数量（瓶/箱）。")
        heard = "".join(segment.text for segment in segments).strip()
        results[name] = {
            "heard": heard,
            "ok": normalized(heard) == normalized(name),
        }
        if index % 25 == 0 or index == len(names):
            print(f"{label} {index}/{len(names)}", flush=True)
    return results


def save_results(payload):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tts-concurrency", type=int, default=8)
    args = parser.parse_args()
    names = customer_names()
    print(f"Testing {len(names)} unique spoken customer names", flush=True)
    asyncio.run(synthesize_all(names, args.tts_concurrency))

    base_hotwords = server._hotwords()
    baseline = transcribe_all(names, base_hotwords, "baseline")
    failures = [name for name in names if not baseline[name]["ok"]]
    with open(FOCUS_FILE, "w") as f:
        json.dump(failures, f, ensure_ascii=False, indent=2)
    print(f"Baseline failures: {len(failures)}/{len(names)}", flush=True)

    focused_hotwords = server._hotwords()
    focused = transcribe_all(names, focused_hotwords, "focused")
    focused_failures = [name for name in names if not focused[name]["ok"]]
    save_results({
        "voice": VOICE,
        "total": len(names),
        "baseline_failure_count": len(failures),
        "focused_failure_count": len(focused_failures),
        "baseline": baseline,
        "focused": focused,
        "focused_failures": focused_failures,
    })
    print(f"Focused failures: {len(focused_failures)}/{len(names)}", flush=True)
    print(f"Results: {RESULTS_FILE}", flush=True)


if __name__ == "__main__":
    main()
