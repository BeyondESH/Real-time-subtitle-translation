"""Packaged-backend WebSocket smoke test for per-process audio capture.

Connects to a running packaged ``SubtitleTranslator.exe`` and exercises:
  1. get_audio_processes  -> supported=true + list shape
  2. get_config           -> audio.source present
  3. spawn tone python.exe, bind it as process source
  4. get_config           -> audio.source.kind == 'process' (proves activation
                             did not silently fall back to device)
  5. kill tone            -> expect ``audio_source_lost`` broadcast + fallback
  6. legacy source_id=''  -> succeeds

Usage::

    python backend/scripts/smoke_packaged_ws.py
    python backend/scripts/smoke_packaged_ws.py --url ws://localhost:8765
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time

import websockets

TONE_CODE = r'''
import time
import numpy as np
import soundcard as sc

sr = 48000
spk = sc.default_speaker()
print("TONE_READY %r" % (spk.name,), flush=True)
t = np.arange(sr * 30) / sr
x = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
spk.play(np.column_stack([x, x]), samplerate=sr)
'''

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = ""):
    RESULTS.append((name, ok, note))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {note}", flush=True)


class WsClient:
    def __init__(self, ws):
        self.ws = ws
        self._seq = 0
        self.buffered: list[dict] = []

    async def request(self, method: str, params=None, timeout: float = 15.0) -> dict:
        self._seq += 1
        rid = f"r{self._seq}"
        await self.ws.send(json.dumps({
            "type": "request", "id": rid, "method": method, "params": params
        }))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"request {method} timed out")
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            if msg.get("type") == "response" and msg.get("id") == rid:
                print(f"  <- response {method}: ok={msg.get('ok')} "
                      f"result={json.dumps(msg.get('result'), ensure_ascii=False)[:400]}"
                      f"{'' if msg.get('ok') else ' error=' + str(msg.get('error'))}",
                      flush=True)
                return msg
            self.buffered.append(msg)

    async def control(self, action: str, **fields):
        payload = {"type": "control", "action": action}
        payload.update(fields)
        await self.ws.send(json.dumps(payload))
        print(f"  -> control {payload}", flush=True)

    async def wait_broadcast(self, mtype: str, timeout: float = 8.0):
        for i, msg in enumerate(self.buffered):
            if msg.get("type") == mtype:
                return self.buffered.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            except asyncio.TimeoutError:
                return None
            if msg.get("type") == mtype:
                print(f"  <- broadcast {mtype}: {json.dumps(msg, ensure_ascii=False)}",
                      flush=True)
                return msg
            self.buffered.append(msg)


async def connect(url: str, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return await websockets.connect(url)
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(0.5)
    raise RuntimeError(f"无法连接后端 {url}: {last}")


async def run(args):
    tone = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-c", TONE_CODE],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    print(f"tone child pid={tone.pid}", flush=True)

    ws = await connect(args.url)
    client = WsClient(ws)
    try:
        # 1) get_audio_processes
        resp = await client.request("get_audio_processes")
        result = resp.get("result") or {}
        ok_shape = (
            resp.get("ok") is True
            and result.get("supported") is True
            and result.get("reason") is None
            and isinstance(result.get("processes"), list)
        )
        record("get_audio_processes supported=true + shape", ok_shape,
               f"supported={result.get('supported')} count={len(result.get('processes') or [])}")

        # 2) get_config audio.source
        resp = await client.request("get_config")
        result = resp.get("result") or {}
        audio = (result.get("audio") or {}).get("source")
        record("get_config.audio.source present", isinstance(audio, dict),
               f"audio.source={audio}")

        # 3) find tone child in enum (poll)
        entry = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            resp = await client.request("get_audio_processes")
            procs = (resp.get("result") or {}).get("processes") or []
            entry = next((p for p in procs if p.get("pid") == tone.pid), None)
            if entry is not None:
                break
            await asyncio.sleep(0.5)
        record("tone process appears in get_audio_processes", entry is not None,
               f"entry={entry}")
        if entry is None:
            return

        # 4) bind process source
        await client.control("set_audio_source",
                             source={"kind": "process", "pid": entry["pid"],
                                     "name": entry["name"]})
        await asyncio.sleep(1.0)
        resp = await client.request("get_config")
        src = ((resp.get("result") or {}).get("audio") or {}).get("source")
        record("process source active after bind (no fallback)", 
               isinstance(src, dict) and src.get("kind") == "process",
               f"audio.source={src}")

        # 5) kill tone -> expect audio_source_lost + fallback to device
        print("killing tone child...", flush=True)
        tone.terminate()
        try:
            tone.wait(timeout=5)
        except Exception:  # noqa: BLE001
            tone.kill()
        lost = await client.wait_broadcast("audio_source_lost", timeout=8.0)
        record("audio_source_lost broadcast after target death", lost is not None,
               f"msg={lost}")
        if lost is not None:
            ok_lost = (lost.get("name") == entry["name"]
                       and lost.get("pid") == entry["pid"]
                       and lost.get("fallback") == "system")
            record("audio_source_lost shape correct", ok_lost, f"msg={lost}")

        await asyncio.sleep(0.5)
        resp = await client.request("get_config")
        src = ((resp.get("result") or {}).get("audio") or {}).get("source")
        record("fallback reset audio.source to device",
               isinstance(src, dict) and src.get("kind") == "device",
               f"audio.source={src}")

        # 6) legacy format
        await client.control("set_audio_source", source_id="")
        await asyncio.sleep(0.5)
        resp = await client.request("get_config")
        src = ((resp.get("result") or {}).get("audio") or {}).get("source")
        record("legacy source_id='' accepted", 
               isinstance(src, dict) and src.get("kind") == "device",
               f"audio.source={src}")
    finally:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
        if tone.poll() is None:
            tone.terminate()
            try:
                tone.wait(timeout=5)
            except Exception:  # noqa: BLE001
                tone.kill()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://localhost:8765")
    args = ap.parse_args()
    asyncio.run(run(args))

    print("\n=== SUMMARY ===")
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, note in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  {note}")
    print(f"=== {len(RESULTS) - len(failed)}/{len(RESULTS)} passed ===")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
