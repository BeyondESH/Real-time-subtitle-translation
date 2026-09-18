/**
 * 打包版后端独立探针 v2（task 5.6 ①"模型首下载路径"的自动化自证）
 *
 * 基于 v1 验尸的三处事实修正：
 * 1. asr_engine 缓存目录 = Path.home()/.cache/subtitle-translator/whisper（download_root
 *    不走 HF_HOME）→ 强制"首次下载"须重定向 USERPROFILE
 * 2. "正在下载模型 (39MB)" 是无条件静态文案 → 不作为下载实证；
 *    真凭据 = 重定向缓存目录出现模型文件 + WS 收到 progress<100 的 model_progress
 * 3. WS bind 先于 ASR 初始化 → get_config 必须轮询至 initialized=true
 *
 * 策略：Phase A 全新缓存（真下载，网络预算 240s）；失败自动降级 Phase B
 * 缓存模式（至少证明打包二进制完整可服务）。
 *
 * 副作用：仅临时目录 + 独立端口 19001；成功即清理，失败保留证据目录。
 *
 * 注（replace-translation-engine-with-llamacpp）：首启除 Whisper 外，后端会以后台任务
 * 预载默认翻译模型（Hy-MT2-1.8B，约 1.1GB，经 model_progress 可见）；本探针仅等待
 * ASR initialized（翻译下载不阻塞服务启动），并顺带断言 get_config 的翻译段新形状。
 */
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const WebSocket = require('ws');

const EXE = path.resolve(
  __dirname, '..', 'release', 'win-unpacked', 'resources',
  'backend', 'SubtitleTranslator', 'SubtitleTranslator.exe'
);
const PORT = 19001;
const WS_URL = `ws://localhost:${PORT}`;

const results = [];
let exitCode = 0;
// --phase-b：Phase A 成功后仍强制继续 Phase B（验证打包 llama-server 就绪与设备上报）
const FORCE_B = process.argv.includes('--phase-b');
const check = (name, ok, extra) => {
  results.push(`${ok ? 'PASS' : 'FAIL'} | ${name}${extra ? ` (${extra})` : ''}`);
  if (!ok) exitCode = 1;
};
const note = (msg) => results.push(`INFO | ${msg}`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const CONFIG_YAML = [
  'audio:',
  '  sample_rate: 16000',
  '  channels: 1',
  '  chunk_size: 1024',
  'pipeline:',
  '  buffer_seconds: 30',
  '  tick_ms: 250',
  '  max_utterance_s: 15',
  '  queue_size: 8',
  'vad:',
  '  threshold: 0.5',
  '  min_silence_duration_ms: 600',
  '  speech_pad_ms: 200',
  'asr:',
  '  model_size: tiny',
  '  device: cpu',
  '  compute_type: int8',
  'translation:',
  '  default_model: hy-mt2-1.8b-q4km',
  '  download:',
  '    source: auto',
  '  target_languages:',
  '    - zh',
  '  device: auto',
  'websocket:',
  '  host: localhost',
  `  port: ${PORT}`,
  ''
].join('\n');

function requestOnce(method, id, timeoutMs = 8000) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(WS_URL);
    const reqId = `probe-${id}`;
    let settled = false;
    const done = (fn, arg) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws.close(); } catch { /* noop */ }
      fn(arg);
    };
    const timer = setTimeout(() => done(reject, new Error('request timeout')), timeoutMs);
    ws.once('open', () => {
      ws.send(JSON.stringify({ type: 'request', id: reqId, method, params: null }));
    });
    ws.on('message', (raw) => {
      let msg;
      try { msg = JSON.parse(raw.toString()); } catch { return; }
      if (msg.type === 'response' && msg.id === reqId) done(resolve, msg);
    });
    ws.once('error', (e) => done(reject, e));
  });
}

/** 轮询 get_config 直到 asr.initialized=true；期间旁听 model_progress */
async function waitInitialized(timeoutMs) {
  const t0 = Date.now();
  let seq = 0;
  while (Date.now() - t0 < timeoutMs) {
    try {
      const resp = await requestOnce('get_config', ++seq);
      if (resp && resp.ok && resp.result && resp.result.asr) {
        if (resp.result.asr.initialized === true) return resp.result;
      }
    } catch { /* 初始化中/端口未就绪，重试 */ }
    await sleep(2500);
  }
  return null;
}

/** 翻译段新形状断言（replace-translation-engine-with-llamacpp：注册表 + 实际设备） */
function checkTranslationShape(result, phase) {
  const tr = (result && result.translation) || {};
  check(`${phase}：translation 段为注册表新形状（model/available_models/resolved_device）`,
    tr.model === 'hy-mt2-1.8b-q4km'
      && Array.isArray(tr.available_models)
      && Object.prototype.hasOwnProperty.call(tr, 'resolved_device')
      && Object.prototype.hasOwnProperty.call(tr, 'device_reason'),
    `model=${tr.model}`);
  check(`${phase}：无 NLLB 旧字段`,
    !('primary_model' in tr) && !('fallback_model' in tr)
      && !('nllb_loaded' in tr) && !('nllb_languages' in tr));
}

/** 轮询至翻译 llama-server 就绪（ready=true），返回最终 result */
async function waitTranslationReady(timeoutMs) {
  const t0 = Date.now();
  let seq = 1000;
  let last = null;
  while (Date.now() - t0 < timeoutMs) {
    try {
      const resp = await requestOnce('get_config', ++seq);
      if (resp && resp.ok && resp.result && resp.result.translation) {
        last = resp.result;
        if (resp.result.translation.ready === true) return last;
      }
    } catch { /* 重试 */ }
    await sleep(2500);
  }
  return last;
}

async function runPhase(label, envOverride, initTimeoutMs) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `backend-probe-${label}-`));
  const logDir = path.join(tmp, 'logs');
  fs.mkdirSync(logDir, { recursive: true });
  const cfgPath = path.join(tmp, 'config.yaml');
  fs.writeFileSync(cfgPath, CONFIG_YAML, 'utf8');

  const child = spawn(EXE, [], {
    cwd: path.dirname(EXE),
    env: {
      ...process.env,
      SUBTITLE_CONFIG_PATH: cfgPath,
      SUBTITLE_LOG_DIR: logDir,
      // 生产由 Electron BackendManager 注入；探针直连 exe 时按 extraResources 结构补位
      SUBTITLE_LLAMA_DIR: path.resolve(path.dirname(EXE), '..', '..', 'llama'),
      ...envOverride(tmp)
    },
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true
  });
  let exited = null;
  let stderrTail = '';
  child.on('exit', (code) => { exited = code; });
  const drain = (d) => { stderrTail = (stderrTail + d.toString()).slice(-1500); };
  child.stderr.on('data', drain);
  child.stdout.on('data', drain);

  const killTree = () => {
    try { spawn('taskkill', ['/pid', String(child.pid), '/T', '/F'], { windowsHide: true }); } catch { /* noop */ }
  };

  try {
    const result = await waitInitialized(initTimeoutMs);
    const backendLogPath = path.join(logDir, 'backend.log');
    const backendLog = fs.existsSync(backendLogPath)
      ? fs.readFileSync(backendLogPath, 'utf8')
      : '';
    return { tmp, result, exited, stderrTail, backendLog, killTree };
  } catch (err) {
    killTree();
    return { tmp, result: null, exited, stderrTail: `${stderrTail}\n${String(err)}`, backendLog: '', killTree };
  }
}

function countFiles(dir) {
  if (!fs.existsSync(dir)) return 0;
  let n = 0;
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      if (e.isDirectory()) walk(path.join(d, e.name));
      else n += 1;
    }
  };
  walk(dir);
  return n;
}

async function main() {
  if (!fs.existsSync(EXE)) {
    check('打包后端 exe 存在', false, EXE);
    return;
  }
  check('打包后端 exe 存在', true);

  // ---------- Phase A：全新缓存（USERPROFILE 重定向 → 真实首次下载） ----------
  const A = await runPhase('fresh', (tmp) => ({
    USERPROFILE: tmp,
    TEMP: tmp,
    TMP: tmp
  }), 240000);

  if (A.result) {
    check('PhaseA 首下载：get_config initialized=true（tiny/cpu/int8）',
      A.result.asr.model_size === 'tiny', JSON.stringify(A.result.asr));
    checkTranslationShape(A.result, 'PhaseA');
    const cacheDir = path.join(A.tmp, '.cache', 'subtitle-translator', 'whisper');
    const nFiles = countFiles(cacheDir);
    check('PhaseA 首下载：重定向缓存目录出现真实模型文件', nFiles > 0, `${cacheDir} files=${nFiles}`);
    check('PhaseA 首下载：backend.log 服务启动完成', A.backendLog.includes('服务启动完成'));
    A.killTree();
    await sleep(1500);
    try { fs.rmSync(A.tmp, { recursive: true, force: true }); } catch { /* noop */ }
    if (!FORCE_B) {
      note('首下载路径已实证，跳过 Phase B');
      return;
    }
    note('首下载路径已实证；--phase-b 强制继续 Phase B');
  } else {
    // ---------- Phase A 失败：留证据，降级 Phase B（缓存模式） ----------
    note(`PhaseA 失败（多为网络不可达 HF）：exited=${A.exited} stderr尾部=${A.stderrTail.slice(-300)}`);
    note(`PhaseA 证据保留: ${A.tmp}`);
    A.killTree();
    await sleep(1500);
  }

  const B = await runPhase('cached', () => ({ SUBTITLE_DEVICE: 'cuda' }), 90000);
  if (B.result) {
    check('PhaseB 缓存模式：打包后端完整启动（模型加载+WS 服务）',
      B.result.asr.model_size === 'tiny' && B.result.asr.initialized === true,
      JSON.stringify(B.result.asr));
    checkTranslationShape(B.result, 'PhaseB');
    // 打包产物 llama-server 按设备拉起（SUBTITLE_DEVICE=cuda；无卡环境允许静默降级）
    const ready = await waitTranslationReady(60000);
    const tr = (ready && ready.translation) || {};
    check('PhaseB：打包 llama-server 就绪（translation.ready=true）', tr.ready === true,
      `resolved=${tr.resolved_device}/${tr.device_reason}`);
    check('PhaseB：实际设备已上报（非空，允许静默降级）',
      tr.resolved_device === 'cuda' || tr.resolved_device === 'cpu',
      `resolved=${tr.resolved_device}/${tr.device_reason}`);
    // 日志在翻译就绪后重读（runPhase 的快照早于 llama-server 启动）
    const logNow = (() => {
      const p = path.join(B.tmp, 'logs', 'backend.log');
      return fs.existsSync(p) ? fs.readFileSync(p, 'utf8') : '';
    })();
    check('PhaseB：backend.log 可见 llama-server 就绪', logNow.includes('llama-server 就绪'));
    check('PhaseB：backend.log 服务启动完成', logNow.includes('服务启动完成'));
    if (!A.result) {
      // 首下载路径无法在本环境自证（网络），如实降级为提示而非 PASS
      note('首下载路径因网络未自证——保留给用户干净环境验收（5.6 ①）');
    }
  } else {
    check('PhaseB 缓存模式：打包后端完整启动', false,
      `exited=${B.exited} stderr尾部=${B.stderrTail.slice(-400)}`);
    note(`PhaseB 证据保留: ${B.tmp}`);
  }
  B.killTree();
  await sleep(1500);
  if (B.result) {
    try { fs.rmSync(B.tmp, { recursive: true, force: true }); } catch { /* noop */ }
  }
}

const alarm = setTimeout(() => {
  console.log(results.join('\n'));
  console.log('PROBE_TIMEOUT');
  process.exit(3);
}, 420000);

main()
  .catch((e) => {
    check('探针执行异常', false, String(e && e.stack).split('\n')[0]);
  })
  .finally(() => {
    clearTimeout(alarm);
    console.log(results.join('\n'));
    console.log(`BACKEND_PROBE_DONE exitCode=${exitCode}`);
    process.exit(exitCode);
  });
