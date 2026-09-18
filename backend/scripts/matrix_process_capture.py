"""Real-machine matrix for per-process audio capture (task 10.1).

Launches the backend **from source** on a scratch port, then exercises the
integration end-to-end. Audio-content evidence (RMS / FFT peak) is measured by
an independent ``process_loopback.ProcessLoopbackCapture`` on the target PID
(coexisting activations are allowed) and by a soundcard loopback recorder for
the device path.

Usage::

    python backend/scripts/matrix_process_capture.py
    python backend/scripts/matrix_process_capture.py --skip-edge
"""
from __future__ import annotations

# comtypes must initialise MTA (soundcard already puts us on MTA)
import sys as _sys
if not hasattr(_sys, "coinit_flags"):
    _sys.coinit_flags = 0

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import psutil
import soundcard as sc
import websockets

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import process_loopback as pl  # noqa: E402
from audio_capture import AudioCapture  # noqa: E402

TONE_CODE = r'''
import numpy as np
import soundcard as sc
sr = 48000
freq = float({freq})
spk = sc.default_speaker()
print("TONE_READY %r %s" % (spk.name, freq), flush=True)
t = np.arange(sr * {seconds}) / sr
x = (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
spk.play(np.column_stack([x, x]), samplerate=sr)
'''

RESULTS: list[tuple[str, bool, str]] = []


def _hz(v) -> str:
    return f"{v:.0f}Hz" if isinstance(v, (int, float)) else str(v)


def record(item: str, ok: bool, evidence: str = ""):
    RESULTS.append((item, ok, evidence))
    print(f"[{'PASS' if ok else 'FAIL'}] {item} :: {evidence}", flush=True)


# --------------------------------------------------------------------------- #
# tone players / audio measurement
# --------------------------------------------------------------------------- #

class TonePlayer:
    def __init__(self, freq: float, exe: str = None, seconds: int = 90):
        self.freq = freq
        self.proc = subprocess.Popen(
            [exe or sys.executable, "-X", "utf8", "-c",
             TONE_CODE.format(freq=freq, seconds=seconds)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )
        self.pid = self.proc.pid
        try:
            self.name = psutil.Process(self.pid).name()
        except Exception:  # noqa: BLE001
            self.name = "python.exe"

    def kill(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


def _rms_and_peak(frames: list[np.ndarray]):
    if not frames:
        return 0.0, None, 0
    audio = np.concatenate(frames)
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    mono = audio.mean(axis=1) if audio.ndim > 1 else audio
    n = min(len(mono), 48000)
    peak = None
    if n > 64:
        win = np.hanning(n)
        spec = np.abs(np.fft.rfft(mono[:n] * win))
        freq = np.fft.rfftfreq(n, 1 / pl.SAMPLE_RATE)
        peak = float(freq[int(np.argmax(spec))])
    return rms, peak, int(audio.shape[0])


def measure_process_capture(pid: int, seconds: float = 2.5):
    """Independent per-process loopback measurement on ``pid`` (INCLUDE tree)."""
    frames: list[np.ndarray] = []

    def cb(audio):
        frames.append(audio)

    cap = pl.ProcessLoopbackCapture(pid, cb)
    cap.start()
    time.sleep(seconds)
    cap.stop()
    rms, peak, total = _rms_and_peak(frames)
    return {"rms": rms, "peak_hz": peak, "samples": total}


def measure_device_loopback(seconds: float = 2.0):
    """Record the system default speaker's loopback (soundcard) → RMS/FFT."""
    default_name = sc.default_speaker().name
    mics = [m for m in sc.all_microphones(include_loopback=True)
            if m.isloopback and m.name == default_name]
    if not mics:
        return {"rms": 0.0, "peak_hz": None, "samples": 0, "device": None}
    frames = []
    with mics[0].recorder(samplerate=48000, channels=2, blocksize=2048) as rec:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            frames.append(rec.record(numframes=2048))
    rms, peak, total = _rms_and_peak(frames)
    return {"rms": rms, "peak_hz": peak, "samples": total, "device": mics[0].name}


def pycaw_states(pid: int):
    try:
        from pycaw.pycaw import AudioUtilities
        return [int(s.State) for s in AudioUtilities.GetAllSessions()
                if s.ProcessId == pid]
    except Exception as exc:  # noqa: BLE001
        return f"<err {exc}>"


# --------------------------------------------------------------------------- #
# backend service + WS client
# --------------------------------------------------------------------------- #

def start_backend(port: int, log_dir: Path, config_path: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["SUBTITLE_CONFIG_PATH"] = str(config_path)
    env["SUBTITLE_LOG_DIR"] = str(log_dir)
    env["SUBTITLE_DEVICE"] = "cpu"
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "main.py"],
        cwd=str(BACKEND), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    return proc


def make_config(port: int, dest: Path):
    import yaml
    src = BACKEND.parent / "config.yaml"
    cfg = yaml.safe_load(src.read_text(encoding="utf-8"))
    cfg.setdefault("websocket", {})["port"] = port
    cfg.setdefault("asr", {})["model_size"] = "tiny"
    dest.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


class WsClient:
    def __init__(self, ws):
        self.ws = ws
        self._seq = 0
        self.buffered: list[dict] = []

    async def request(self, method, params=None, timeout=15.0):
        self._seq += 1
        rid = f"r{self._seq}"
        await self.ws.send(json.dumps(
            {"type": "request", "id": rid, "method": method, "params": params}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=remaining))
            if msg.get("type") == "response" and msg.get("id") == rid:
                return msg
            self.buffered.append(msg)

    async def control(self, action, **fields):
        payload = {"type": "control", "action": action}
        payload.update(fields)
        await self.ws.send(json.dumps(payload))

    async def wait_broadcast(self, mtype, timeout=8.0):
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
                return msg
            self.buffered.append(msg)


async def connect_ws(url, timeout=30.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return await websockets.connect(url)
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(0.4)
    raise RuntimeError(f"connect failed: {last}")


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #

async def main(args):
    work = Path(tempfile.mkdtemp(prefix="matrix_proc_"))
    log_dir = work / "logs"
    log_dir.mkdir()
    config_path = work / "config.yaml"
    make_config(args.port, config_path)
    log_path = log_dir / "backend.log"
    url = f"ws://localhost:{args.port}"

    def log_text():
        return log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""

    def wait_log(pattern, timeout=8.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pattern in log_text():
                return True
            time.sleep(0.2)
        return False

    backend = start_backend(args.port, log_dir, config_path)
    warm = await connect_ws(url)  # warm-up: waits until listening
    await warm.close()
    print(f"backend spawned pid={backend.pid}; log={log_path}", flush=True)

    # controlled twin interpreter: identical exe name for both tones, so the
    # same-name group contains exactly two members (avoids unrelated python.exe
    # sessions in the user environment).
    tone_exe = work / "toneprobe.exe"
    try:
        import shutil
        shutil.copyfile(sys.executable, tone_exe)
    except Exception as exc:  # noqa: BLE001
        print(f"(twin copy failed: {exc}; falling back to python.exe)", flush=True)
        tone_exe = Path(sys.executable)
    tone_name = tone_exe.name

    tones: list[TonePlayer] = []
    edge_proc = None
    try:
        ws = await connect_ws(url)
        client = WsClient(ws)

        # ---- wait until device capture started ----
        wait_log("开始音频捕获（设备回环）", timeout=30)
        record("setup. source backend starts + listens", backend.poll() is None,
               f"pid={backend.pid} log_has_device_capture="
               f"{'开始音频捕获（设备回环）' in log_text()}")

        # ================= Item 1: dual same-name instances =================
        t440 = TonePlayer(440.0, exe=str(tone_exe))
        time.sleep(1.0)
        t880 = TonePlayer(880.0, exe=str(tone_exe))
        tones = [t440, t880]
        time.sleep(1.0)

        entries = []
        all_same_name = []
        for _ in range(30):
            resp = await client.request("get_audio_processes")
            procs = (resp.get("result") or {}).get("processes") or []
            all_same_name = [p for p in procs if p.get("name") == tone_name]
            entries = [p for p in all_same_name if p.get("pid") in (t440.pid, t880.pid)]
            if len(entries) == 2:
                break
            await asyncio.sleep(0.5)

        ords = {p["pid"]: p.get("ordinal") for p in entries}
        order = [p["pid"] for p in entries]
        expected_order = sorted(
            [p["pid"] for p in entries],
            key=lambda pid: (not next(bool(p["active"]) for p in entries
                                      if p["pid"] == pid), pid),
        )
        record("1a. dual same-name listed with ordinals 1/2",
               len(entries) == 2 and sorted(ords.values()) == [1, 2]
               and order == expected_order,
               f"tone_name={tone_name} all_same_name_group="
               f"{[(p['pid'], p.get('active'), p.get('ordinal')) for p in all_same_name]} "
               f"our_entries={[(p['pid'], p.get('active'), p.get('ordinal')) for p in entries]}")

        # bind instance #2 (ordinal 2)
        inst2 = next((p for p in entries if p.get("ordinal") == 2), None)
        inst1 = next((p for p in entries if p.get("ordinal") == 1), None)
        if inst2 is not None:
            await client.control("set_audio_source",
                                 source={"kind": "process", "pid": inst2["pid"],
                                         "name": inst2["name"]})
            await asyncio.sleep(1.0)
            resp = await client.request("get_config")
            src = ((resp.get("result") or {}).get("audio") or {}).get("source")
            rec2 = measure_process_capture(inst2["pid"], 2.5)
            rec1 = measure_process_capture(inst1["pid"], 2.0) if inst1 else {"peak_hz": None, "rms": 0.0}
            expected_freq = 880.0 if inst2["pid"] == t880.pid else 440.0
            other_freq = 440.0 if expected_freq == 880.0 else 880.0
            backend_bound = f"开始音频捕获（进程 {tone_name} pid={inst2['pid']}）" in log_text()
            record("1b. bind instance #2 → only its frequency (FFT)",
                   bool(src) and src.get("pid") == inst2["pid"]
                   and rec2["peak_hz"] is not None
                   and abs(rec2["peak_hz"] - expected_freq) <= 20
                   and rec1["peak_hz"] is not None
                   and abs(rec1["peak_hz"] - other_freq) <= 20
                   and rec2["rms"] > 0.02,
                   f"bound={src} inst2_peak={_hz(rec2['peak_hz'])} rms={rec2['rms']:.3f} "
                   f"inst1_peak={_hz(rec1['peak_hz'])} backend_log_bound={backend_bound}")

        # ================= Item 6: active-flag sanity =================
        samples = []
        for _ in range(10):
            resp = await client.request("get_audio_processes")
            procs = (resp.get("result") or {}).get("processes") or []
            e = next((p for p in procs if p.get("pid") == t880.pid), None)
            samples.append((e.get("active") if e else None, pycaw_states(t880.pid)))
            await asyncio.sleep(0.3)
        any_active = any(s[0] is True for s in samples)
        states = [s[1] for s in samples]
        record("6. active flag sanity while audibly playing",
               True,  # observational; reported, not pass/fail on value
               f"service_active_samples={[s[0] for s in samples]} pycaw_state_samples={states}")

        # ================= Item 3: pause/resume (in-process, real capture) ===
        pause_tone = TonePlayer(660.0, exe=str(tone_exe))
        tones.append(pause_tone)
        time.sleep(1.0)
        chunks: list[np.ndarray] = []

        def on_chunk(a):
            chunks.append(a)

        cap = AudioCapture({"audio": {}})
        cap.set_audio_source({"kind": "process", "pid": pause_tone.pid,
                              "name": pause_tone.name})
        await cap.start(on_chunk)
        await asyncio.sleep(1.5)
        rms_before, _, _ = _rms_and_peak(chunks)
        cap.pause()
        n0 = len(chunks)
        await asyncio.sleep(1.5)
        n_paused = len(chunks) - n0
        cap.resume()
        n1 = len(chunks)
        await asyncio.sleep(1.5)
        rms_after, _, _ = _rms_and_peak(chunks[n1:])
        await cap.stop()
        record("3. pause drops / resume resumes (in-process real capture)",
               rms_before > 0.02 and n_paused == 0 and rms_after > 0.02,
               f"rms_before={rms_before:.3f} chunks_during_pause={n_paused} "
               f"rms_after_resume={rms_after:.3f}")

        # ================= Item 4: runtime switch process ↔ device ==========
        await client.control("set_audio_source", source={"kind": "device", "id": ""})
        await asyncio.sleep(1.5)
        resp = await client.request("get_config")
        src_dev = ((resp.get("result") or {}).get("audio") or {}).get("source")
        await client.control("set_audio_source",
                             source={"kind": "process", "pid": inst2["pid"],
                                     "name": inst2["name"]})
        await asyncio.sleep(1.5)
        resp = await client.request("get_config")
        src_proc = ((resp.get("result") or {}).get("audio") or {}).get("source")
        log = log_text()
        record("4. runtime switch process→device→process",
               src_dev and src_dev.get("kind") == "device"
               and src_proc and src_proc.get("kind") == "process"
               and src_proc.get("pid") == inst2["pid"]
               and log.count("开始音频捕获（设备回环）") >= 2
               and f"开始音频捕获（进程 {tone_name} pid={inst2['pid']}）" in log,
               f"device_src={src_dev} proc_src={src_proc} "
               f"device_restarts={log.count('开始音频捕获（设备回环）')}")

        # ================= Item 2: browser child-tree (best effort) =========
        if not args.skip_edge:
            await scenario_edge(client, log_text, work, record)
        else:
            record("2. browser child-tree", False, "skipped via --skip-edge")

        # ================= Item 5: exit fallback + Fix verification =========
        # rebind t880, kill it, expect fallback + Realtek default in log,
        # and device loopback flows while a separate tone plays.
        await client.control("set_audio_source",
                             source={"kind": "process", "pid": t880.pid,
                                     "name": t880.name})
        await asyncio.sleep(1.2)
        t880.kill()
        tones.remove(t880)
        lost = await client.wait_broadcast("audio_source_lost", timeout=6.5)
        time.sleep(0.6)
        log = log_text()
        fix_ok = "使用默认回环设备: 扬声器 (Realtek(R) Audio)" in log
        # device loopback flows during fallback window
        ambient = TonePlayer(550.0)
        tones.append(ambient)
        time.sleep(0.6)
        dev = measure_device_loopback(2.0)
        ambient.kill()
        tones.remove(ambient)
        record("5. exit fallback ≤6s + Part-A default = Realtek",
               lost is not None and lost.get("fallback") == "system"
               and fix_ok and dev["rms"] > 0.02,
               f"lost={lost} fix_log={fix_ok} device_loopback={dev}")

        # ================= Item 7: old-system gate ==========================
        record("7. OS gate build<19041", False,
               "UNVERIFIED-BY-HARDWARE (no old Windows build available; "
               "covered by unit tests only)")

        await ws.close()
    finally:
        for t in tones:
            t.kill()
        if edge_proc is not None:
            kill_edge_tree(edge_proc.pid)
        if backend.poll() is None:
            backend.terminate()
            try:
                backend.wait(timeout=8)
            except Exception:  # noqa: BLE001
                backend.kill()
        # keep evidence log outside the repo (temp dir)
        try:
            dest = work / "backend.log"
            dest.write_text(log_text(), encoding="utf-8")
            print(f"backend.log copied to {dest}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"(could not copy backend.log: {exc})", flush=True)

    print("\n=== MATRIX SUMMARY ===")
    for item, ok, ev in RESULTS:
        print(f"  [{'PASS' if ok else 'FAIL'}] {item} :: {ev}")
    hard_fail = [r for r in RESULTS if not r[1]
                 and not r[0].startswith("7.")]
    return 0 if not hard_fail else 1


def kill_edge_tree(pid: int):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15)
    except Exception:  # noqa: BLE001
        pass


async def scenario_edge(client, log_text_fn, work: Path, record):
    edge = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
    if not edge.exists():
        record("2. browser child-tree", False, "msedge.exe not found")
        return
    wav = (BACKEND / "scripts" / "spike_out.wav").as_uri()
    html = work / "play.html"
    html.write_text(
        f'<html><body><audio autoplay loop src="{wav}"></audio></body></html>',
        encoding="utf-8",
    )
    user_data = work / "edge-profile"
    before = set(psutil.pids())
    proc = subprocess.Popen([
        str(edge),
        "--autoplay-policy=no-user-gesture-required",
        "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={user_data}",
        "--new-window", html.as_uri(),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.pid  # noqa: B018

    # descendants of the launched process
    def descendants(root):
        out = set()
        try:
            p = psutil.Process(root)
            for c in p.children(recursive=True):
                out.add(c.pid)
        except Exception:  # noqa: BLE001
            pass
        return out

    listed = []
    try:
        for _ in range(60):
            time.sleep(0.5)
            resp = await client.request("get_audio_processes")
            procs = (resp.get("result") or {}).get("processes") or []
            fam = descendants(proc.pid) | {proc.pid}
            listed = [p for p in procs
                      if p["name"].lower() == "msedge.exe" and p["pid"] in fam]
            if listed:
                break
        if not listed:
            record("2. browser child-tree", False,
                   f"no msedge.exe session for launched tree pid={proc.pid}; "
                   f"all msedge listed={[p['pid'] for p in (resp.get('result') or {}).get('processes', []) if p['name'].lower()=='msedge.exe']}")
            return
        best = None
        for p in listed:
            m = measure_process_capture(p["pid"], 2.0)
            if best is None or m["rms"] > best[1]["rms"]:
                best = (p, m)
        p, m = best
        await client.control("set_audio_source",
                             source={"kind": "process", "pid": p["pid"],
                                     "name": p["name"]})
        await asyncio.sleep(1.0)
        resp = await client.request("get_config")
        src = ((resp.get("result") or {}).get("audio") or {}).get("source")
        record("2. browser child-tree captures audio (RMS>0)",
               m["rms"] > 0.005 and src and src.get("pid") == p["pid"],
               f"listed_msedge={[q['pid'] for q in listed]} bound={src} "
               f"rms={m['rms']:.4f} peak_hz={m['peak_hz']}")
    finally:
        kill_edge_tree(proc.pid)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18766)
    ap.add_argument("--skip-edge", action="store_true")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args)))
