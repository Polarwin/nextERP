"""Run the cached eval audio through SenseVoice and compare with Whisper.

Read-only: reuses voice_customer_eval/order_audio + test cases, runs the same
_fast_parse scoring, writes results to voice_customer_eval/sensevoice_results.json.
"""
import hashlib
import json
import os
import subprocess
import tempfile
import wave

import numpy as np
import sherpa_onnx

import server
import voice_customer_eval as vce

MODEL_DIR = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"
OUT_FILE = os.path.join(vce.OUT_DIR, "sensevoice_results.json")

_recognizer = None


def recognizer():
    global _recognizer
    if _recognizer is None:
        _recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=f"{MODEL_DIR}/model.int8.onnx",
            tokens=f"{MODEL_DIR}/tokens.txt",
            num_threads=4, use_itn=True, language="zh")
    return _recognizer


def transcribe(mp3_path):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as w:
        wav = w.name
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3_path,
                        "-ar", "16000", "-ac", "1", "-f", "wav", wav],
                       check=True)
        with wave.open(wav) as wf:
            samples = np.frombuffer(
                wf.readframes(wf.getnframes()),
                dtype=np.int16).astype(np.float32) / 32768
    finally:
        os.unlink(wav)
    stream = recognizer().create_stream()
    stream.accept_waveform(16000, samples)
    recognizer().decode_stream(stream)
    return stream.result.text.strip()


def main():
    cases = vce.test_cases() + vce.item_test_cases()
    with server._cache_lock:
        customers = list(server._cache["customers"])
        items = list(server._cache["items"])
    try:
        with open(OUT_FILE, encoding="utf-8") as f:
            results = json.load(f)
    except (OSError, ValueError):
        results = {}
    for index, case in enumerate(cases, 1):
        key = hashlib.sha256(case["sentence"].encode()).hexdigest()[:20]
        if key in results:
            continue
        path = vce.audio_path(case["sentence"])
        if not os.path.exists(path):
            continue
        heard = server._normalize_transcript_text(transcribe(path))
        parsed = server._fast_parse(heard, customers, items)
        actual_customer = ((parsed or {}).get("customer") or {}).get("name")
        parsed_items = (parsed or {}).get("items") or []
        acceptable = case.get("expected_item_codes") or \
            [case["expected_item_code"]]
        item_match = any(row["item_code"] in acceptable
                         and row["qty"] == case["quantity"]
                         for row in parsed_items)
        customer_match = actual_customer == case["expected_customer_id"]
        results[key] = {
            **case,
            "heard": heard,
            "actual_customer_id": actual_customer,
            "actual_items": [{"item_code": r["item_code"], "qty": r["qty"]}
                             for r in parsed_items],
            "customer_match": customer_match,
            "item_match": item_match,
            "matched": customer_match and item_match,
        }
        if index % 10 == 0 or index == len(cases):
            with open(OUT_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            matched = sum(1 for r in results.values() if r["matched"])
            print(f"SenseVoice {index}/{len(cases)} "
                  f"(matched so far {matched}/{len(results)})", flush=True)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    matched = sum(1 for r in results.values() if r["matched"])
    print(f"SenseVoice total: {matched}/{len(results)}", flush=True)


if __name__ == "__main__":
    main()
