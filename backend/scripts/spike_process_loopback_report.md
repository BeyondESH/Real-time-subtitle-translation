# Spike Report — Per-Process WASAPI Loopback Capture (tasks 1.1–1.4)

Change: `add-per-process-audio-capture` · Machine: real Windows dev box · Date: 2026-09-18
Author: spike agent · Status: **verification-first, evidence over assumptions**

## 0. Environment (observed)

| Item | Value (raw) |
|------|-------------|
| Python | **3.14.6** (`C:\Python314\python.exe`) — no project venv |
| OS | `Windows-11-10.0.26200-SP0` |
| Windows build | `sys.getwindowsversion(major=10, minor=0, build=26200, platform=2, service_pack='')` → **26200** (gate ≥ 19041 ✅) |
| comtypes | **1.4.16** (installed via pip) |
| pycaw | **20251023** (installed via pip; `pycaw.__version__` not exposed) |
| psutil | **7.2.2** (already present, now pinned) |
| soundcard | 0.4.6 (pre-existing) |

Audio endpoints (`soundcard.all_speakers()`): 3 present; **default = `扬声器 (Realtek(R) Audio)`**.
So there **is** a real default render endpoint (no "no audio device" limitation).

Installed with:
```
python -m pip install comtypes pycaw
# comtypes-1.4.16, pycaw-20251023 installed; "Requirement already satisfied: psutil ... (7.2.2)"
python -m pip show comtypes pycaw psutil
#   comtypes 1.4.16 ; pycaw 20251023 (Requires: comtypes, psutil) ; psutil 7.2.2
```

## 1. Deliverables

| Path | Purpose |
|------|---------|
| `backend/requirements.txt` | + `comtypes>=1.4.16`, `pycaw>=20251023`, `psutil>=7.2.2` under a new "Per-process audio capture" section |
| `backend/scripts/spike_process_loopback.py` | self-contained runnable spike (tone player → pycaw enumeration → ctypes/comtypes activation → capture → RMS/PASS-FAIL → WAV) |
| `backend/scripts/spike_out.wav` | captured 4.0 s of the target's audio (48 kHz stereo, 16-bit PCM container) for manual listening |
| `backend/scripts/spike_process_loopback_report.md` | this report |

Canonical run: `python -X utf8 backend/scripts/spike_process_loopback.py --duration 4`

## 2. Per-item validation status

Legend: **PASS** = claim/behavior reproduced · **FAIL(digest)** = digest claim **not reproduced** (this is a finding, not broken code).

| Item | Status | Observed evidence |
|------|--------|-------------------|
| a. literal `VAD\Process_Loopback` activation | **PASS** | `device='VAD\\Process_Loopback'`, `activate_async_hr=S_OK`, `get_activate_result_hr=S_OK` |
| a-neg. GUID string rejected | **PASS** | all 3 GUID variants rejected: `{1CB9AD4C-…}` → `0x80070057`; endpoint-id → `0x80070002`; bare GUID → `0x80070002`. **Digest predicted `0x8000000E` — wrong (see §4.1)** |
| b. handler implements both interfaces | **PASS** | `handler ifaces = ['IActivateAudioInterfaceCompletionHandler', 'IAgileObject']` |
| b-neg. missing `IAgileObject` → `0x8000000E` | **PASS** | `activate_async_hr = E_ILLEGAL_METHOD_CALL (0x8000000E)` (returned **synchronously**) |
| c. capture thread MTA | **PASS** | `coinit_hr=S_OK`, `apartment=MTA (qualifier=0)` |
| c-neg. STA must fail (digest) | **FAIL(digest)** | STA thread: `apartment=STA`, `hr=S_OK`, `ok=True`, `packets=100`, `rms≈0.209` → **STA works** on 26200 with an agile handler |
| d. buffers in requested format | **PASS** | requested `(48000, 2, 32)`; `packets=400`, `frames=192000`, `bytes=1536000`, expected `1536000`, **ratio=1.000** (packet = 480 frames = **10 ms**) |
| d2. captured audio is the tone | **PASS** | `rms=0.2112`, `peak=0.3048`; WAV FFT → **dominant 440 Hz**, 4.0 s |
| e. `AUDCLNT_BUFFERFLAGS_SILENT` observable | **FAIL(digest)** | during tone gap: `silent_packets=0 of 399`, `flags_histogram={'0x0': 399}`, `rms=0.00000` (real zeros, no flag) |
| e2. SILENT flag after target exit | **FAIL(digest)** | `silent_packets=0 of 499`, `flags_histogram={'0x0': 499}` (silence delivered as real samples) |
| f+. re-activation of same PID after full release | **PASS** | second activation `S_OK`, `packets=199` |
| f-reg. not releasing async op blocks re-activation (digest) | **FAIL(digest)** | AddRef'd async-op + activated + both clients (4 refs held), re-activate → `S_OK`. Also re-activate while **previous stream still running, nothing released** → `S_OK` |
| g. Windows build ≥ 19041 | **PASS** | build `26200` |
| 4a. pycaw enumerates target render session | **PASS** | child PID session `State=Active` appears during playback, disappears after exit |
| D6. stream survives target exit | **PASS** | killed target 1.5 s into 5 s capture → still `packets=499`, `rms=0.1147` (= 1.5 s tone + 3.5 s silence), no end-of-stream |

Raw summary block from the canonical run:
```
[PASS] g. Windows build >= 19041  build=26200
[PASS] 4a. pycaw enumerates target render session  pid=75836 sessions=1
[PASS] a. literal VAD\Process_Loopback activation  hr=S_OK (0x00000000)
[PASS] b. handler implements IAgileObject + completion
[PASS] c. capture thread apartment = MTA  apt=MTA (qualifier=0)
[PASS] d. buffers in requested 48k/2ch/f32 format  ratio=1.000 (expected≈1536000 bytes)
[PASS] d2. captured audio is non-silent tone (rms>0.02)  rms=0.2112 peak=0.3048
[PASS] f+. re-activation of same PID succeeds after release
[FAIL] e (digest claim). SILENT (0x2) flag observable during a tone gap  silent_packets=0 of 399
[PASS] a-neg. GUID device strings rejected  IID=0x80070057; endpoint-id=0x80070002; bare=0x80070002
[PASS] b-neg. missing IAgileObject -> E_ILLEGAL_METHOD_CALL  hr=0x8000000E
[FAIL] c-neg (digest claim). STA apartment FAILS  ok=True hr=S_OK apt=STA  (claim NOT reproduced)
[FAIL] f-reg (digest claim). re-activation WITHOUT async-op release FAILS  hr=S_OK  (claim NOT reproduced)
[PASS] D6. stream survives target exit (keeps delivering packets)  packets=499
[FAIL] e2 (digest claim). SILENT (0x2) flag observable after target exit  silent_packets=0 of 499
```

## 3. Raw evidence excerpts

### 3.1 Main capture (Scenario A)
```
coinit_hr   = S_OK (0x00000000)
apartment   = MTA (qualifier=0)
device      = 'VAD\\Process_Loopback'
handler ifaces = ['IActivateAudioInterfaceCompletionHandler', 'IAgileObject']
activate_async_hr = S_OK (0x00000000)
get_activate_result_hr = S_OK (0x00000000)
init hns_buffer used = 0
requested_format = (48000, 2, 32)
GetMixFormat     = <GetMixFormat failed: (-2147467263, '尚未实现', (...))>   # E_NOTIMPL
packets=400 frames=192000 bytes=1536000 silent_packets=0
flags_histogram={'0x0': 400}
rms=0.21122 peak=0.30477
expected_bytes~1536000 ratio=1.000
wrote ...\spike_out.wav (384000 samples, 1536000 raw bytes)
```
Independent WAV check:
```
channels 2 width 2 rate 48000 frames 192000 duration_s 4.0 rms 6920.4 peak 9826.0 dominant_hz 440.0
```

### 3.2 GUID device strings (async result, not the initial call)
```
IID_IAudioClient   device='{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}' -> activate_async=S_OK  result=E_INVALIDARG (0x80070057)
endpoint-id style  device='{0.0.0.00000000}.{69f8a1f7-…}'        -> activate_async=S_OK  result=0x80070002
bare GUID          device='94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90' -> activate_async=S_OK  result=0x80070002
```

### 3.3 Missing IAgileObject
```
activate_async_hr = E_ILLEGAL_METHOD_CALL (0x8000000E)
get_activate_result_hr = S_OK (0x00000000)
hr=E_ILLEGAL_METHOD_CALL (0x8000000E)
```

### 3.4 STA apartment
```
coinit_hr = S_OK (0x00000000)
apartment = STA (qualifier=0)
hr=S_OK (0x00000000) ok=True
packets=100 rms=0.20867
```

### 3.5 No-release re-activation (focus probe, first stream still running)
```
first activation: S_OK (0x00000000)
second activation (stream still running, nothing released): S_OK (0x00000000)
third activation after Stop+release: S_OK (0x00000000)
```

### 3.6 Session enumeration (pycaw + psutil)
Baseline (9 sessions) — same-name duplicate sessions on one PID are real:
```
PID=26648 State=Inactive SysSounds=False Name='AMPLibraryAgent.exe'
PID=26648 State=Active   SysSounds=False Name='AMPLibraryAgent.exe'
PID=0     State=Inactive SysSounds=True  Name='<PID 0>' DisplayName='@%SystemRoot%\\System32\\AudioSrv.Dll,-202'
PID=13964 State=Inactive SysSounds=False Name='msedge.exe'
PID=73476 State=Inactive SysSounds=False Name='chrome.exe'
```
Lifecycle probe (target spawned but sleeping vs playing):
```
--- BEFORE first playback (child sleeping): total=9  target_pid=69832 session_count=0
--- AFTER winsound.MessageBeep():            total=9  PID0 State=0 -> 1 (Active), target session_count=0
--- DURING playback:                         total=10 target_pid=69832 session_count=1 states=[1]
--- AFTER child exit:                        total=9  target_pid=69832 session_count=0
```
Name resolution works for real apps (`chrome.exe`, `msedge.exe`, `AppleMusic.exe`, `steam.exe`, `MyPopo.exe`, `POPOMeeting.exe`); most app `DisplayName` is `''`, only the system-sounds session carries the AudioSrv resource string.
**No naturally-occurring same-name / different-PID pair** was present; only same-PID multi-session (`AMPLibraryAgent.exe`). Production same-name ordinals will therefore be unit-tested with injected fake sessions.

## 4. Surprises / deltas vs the technical digest

### 4.1 GUID string HRESULT (digest: `0x8000000E`)
Not reproduced. Passing a GUID string returns `S_OK` from `ActivateAudioInterfaceAsync` (the call is asynchronous) and the failure surfaces at **`GetActivateResult`**:
- `{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}` (valid GUID form) → **`E_INVALIDARG 0x80070057`**
- `{0.0.0.00000000}.{…}` endpoint id → **`0x80070002`** (ERROR_FILE_NOT_FOUND)
- bare GUID without braces → **`0x80070002`**

### 4.2 `IAgileObject` requirement (confirmed, but stage differs from prose)
Missing `IAgileObject` yields exactly `E_ILLEGAL_METHOD_CALL 0x8000000E`, but it is returned **synchronously from `ActivateAudioInterfaceAsync`** (before any completion wait). This matches the TalkTrack source comment; it is *not* an `Initialize`-time error.

### 4.3 MTA vs STA (design D3: "STA 线程直接失败")
Not reproduced. On build 26200 with an agile handler, a **STA** thread activated, started, and captured real audio (`rms≈0.209`, 100 packets). Recommendation remains MTA (matches docs/references; avoids STA message-pump hazards), but STA is not a hard failure here.

### 4.4 `GetMixFormat` on the process-loopback client → `E_NOTIMPL`
`audio_client.GetMixFormat()` returned `0x80004001 E_NOTIMPL`. So the client cannot even report a mix format; the "use requested format, never GetMixFormat" rule is not merely advisory — it is the only option. Requested `48k/2ch/float32` was honored exactly (ratio 1.000, 10 ms packets).

### 4.5 `AUDCLNT_BUFFERFLAGS_SILENT` (digest: observed during gaps/start/end)
Never observed. During a 4 s window spanning a real playback gap (`rms=0.0`, flags all `0x0`) and during 3.5 s after the target process died (`rms` consistent with silence, flags all `0x0`), packets carried **real zero-valued float samples with `flags=0`**. The 0x2 path must stay implemented defensively, but silence must be detected by RMS/VAD, not by this flag.

### 4.6 Async-operation `Release()` not required for re-activation (design D3: "否则同一 PID 无法重新激活")
Not reproduced on this build/stack:
- re-activate after full GC release → `S_OK`;
- re-activate with AddRef'd async-op + activated + both clients held → `S_OK`;
- re-activate while the previous stream is **still running and nothing released** → `S_OK`.
Releasing is still good hygiene (avoid leaks / duplicate capture streams), but it is **not a correctness gate** on Windows 11 build 26200 + comtypes 1.4.16.

### 4.7 pycaw 20251023 API
`pycaw.utils.AudioSession` **does not expose `IsSystemSoundsSession()`**. It must be called on the underlying control: `session._ctl.IsSystemSoundsSession()` (`== 0` / `S_OK` means system sounds). Detect by `PID == 0` **and** that call. `pycaw.__version__` is also absent.

### 4.8 comtypes release semantics
comtypes 1.4.16 COM pointers release via `__del__` (`_compointer_base.__del__` → `Release()`). "Releasing interfaces" in Python = drop references + `gc.collect()`; calling `Release()` manually AND letting `__del__` run would double-release.

## 5. Recommendations for production `backend/process_loopback.py`

1. **Activation**
   - Pass the literal `"VAD\\Process_Loopback"` (never a GUID).
   - Pack `AUDIOCLIENT_ACTIVATION_PARAMS{ActivationType=1, {TargetProcessId=pid, Mode=0}}` into `PROPVARIANT(vt=VT_BLOB)`.
   - Implement the completion handler with **both** `IActivateAudioInterfaceCompletionHandler` and `IAgileObject`; on `QueryInterface`/activation failure surface `0x8000000E` with an actionable message.
   - **Check `GetActivateResult`'s HRESULT** — `ActivateAudioInterfaceAsync` returns `S_OK` even for invalid device strings; map `0x80070057`/`0x80070002` → "invalid capture target/device".
2. **Threading**: run activation+capture on a dedicated **MTA** thread (`CoInitializeEx(None, COINIT_MULTITHREADED)`), keep the completion event + Win32 event pattern. (STA also worked, but MTA is the documented/reference-safe choice.)
3. **Initialize**: `SHARED`, `LOOPBACK | EVENTCALLBACK`, `hnsBufferDuration=0`, `periodicity=0`, requested `WAVE_FORMAT_IEEE_FLOAT 48000/2ch/32-bit`. Keep `2_000_000` only as a defensive fallback (unused in this run).
4. **Format**: trust the requested format; do **not** call `GetMixFormat` on this client (E_NOTIMPL). Expect **480-frame / 10 ms** packets; feed the float32 stereo buffer to the reused `_process_audio(audio, 48000)`.
5. **Silence**: keep the `flags & 0x2` → zero-fill branch, but do not rely on it; silence arrives as real zeros. Use RMS/VAD for "listening" semantics; new-session watchdog is external.
6. **Lifecycle**: `Stop()` then drop all refs (`gc.collect()`); do not assume an async-op `Release()` is required for re-activation. Guard against duplicate concurrent captures (they are technically allowed — a second activation for the same PID returned `S_OK`).
7. **Enumeration**: `pycaw.AudioUtilities.GetAllSessions()`; exclude `ProcessId == 0` **or** `session._ctl.IsSystemSoundsSession() == 0`; dedupe by PID (same-PID multi-session is real); name via `psutil.Process(pid).name()` with `未知进程 (PID)` fallback; `DisplayName` is usually empty and the system-sounds one is a raw `@%SystemRoot%\System32\AudioSrv.Dll,-…` resource string (do not display it).
8. **Gate**: `supported()` = `sys.getwindowsversion().build >= 19041` (26200 here), platform == Windows.

## 6. Scope / cleanliness

- Files changed: `backend/requirements.txt` (M) and new `backend/scripts/spike_process_loopback.py`, `backend/scripts/spike_out.wav`, this report. No openspec/design/production/frontend files touched. Nothing staged or committed.
- Tone-player child processes were terminated at the end of every run; no stray players remain (verified).
- Note: `git status` shows many pre-existing frontend modifications that are **not** part of this spike.
