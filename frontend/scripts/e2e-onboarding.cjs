/**
 * 首启引导流程 E2E（task 5.6 "首启引导"子项自动化）
 *
 * 假后端（端口 18998，与 e2e-smoke 隔离）应答 get_audio_sources / get_config，
 * 并按脚本节奏注入 model_progress 驱动下载页 → 验证：
 *   欢迎页 → 音频源页（真实 wsRequest 设备枚举）→ 下载进度页（进度条驱动 +
 *   100% 后自动前进）→ 完成页 → 直播主界面 + onboarding.completed 落库；
 *   以及"跳过引导"路径。
 *
 * 用法：node scripts/e2e-onboarding.cjs   （E2E_EXE 可指定已安装产物路径）
 * 副作用：临时改写 store（websocket.port / onboarding.completed），结束原样还原。
 */
const { _electron } = require('playwright-core');
const { WebSocketServer } = require('ws');
const fs = require('fs');
const path = require('path');

const FAKE_PORT = 18998;
const STORE_PATH = path.join(
  process.env.APPDATA, 'real-time-subtitle-translator', 'config.json'
);
const EXE = process.env.E2E_EXE
  ? path.resolve(process.env.E2E_EXE)
  : path.resolve(__dirname, '..', 'release', 'win-unpacked', '实时字幕翻译.exe');

const alarm = setTimeout(() => {
  console.log('E2E_ONBOARDING_TIMEOUT');
  process.exit(3);
}, 150000);

const results = [];
let exitCode = 0;
function check(name, ok, extra) {
  results.push(`${ok ? 'PASS' : 'FAIL'} | ${name}${extra ? ` (${extra})` : ''}`);
  if (!ok) exitCode = 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  // ---------- 假后端 ----------
  const received = [];
  let client = null;
  const wss = new WebSocketServer({ port: FAKE_PORT });
  wss.on('connection', (ws) => {
    client = ws;
    ws.on('message', (raw) => {
      let msg;
      try { msg = JSON.parse(raw.toString()); } catch { return; }
      received.push(msg);
      if (msg.type === 'request') {
        const result = msg.method === 'get_audio_sources'
          ? [{ id: 'fake-dev', name: '假扬声器 (E2E)', is_loopback: true }]
          : msg.method === 'get_config'
            ? {
                asr: {
                  model_size: 'base', device: 'auto',
                  resolved_device: 'cpu', device_reason: 'no_cuda'
                },
                translation: { resolved_device: 'cpu', device_reason: 'no_cuda' },
                active_language: 'zh'
              }
            : null;
        ws.send(JSON.stringify({ type: 'response', id: msg.id, ok: true, result }));
      }
    });
  });
  const send = (o) => { if (client) client.send(JSON.stringify(o)); };

  // ---------- store：端口指向假后端 + 恢复"未完成引导"态 ----------
  const storeBackup = fs.readFileSync(STORE_PATH, 'utf8');
  const storeJson = JSON.parse(storeBackup);
  storeJson.websocket = { ...(storeJson.websocket || {}), host: 'localhost', port: FAKE_PORT };
  storeJson.onboarding = { completed: false };
  fs.writeFileSync(STORE_PATH, JSON.stringify(storeJson, null, 2));

  let app = null;
  try {
    app = await _electron.launch({ executablePath: EXE });

    // 等主窗口（URL 导航完成）
    let main_ = null;
    const t0 = Date.now();
    while (!main_ && Date.now() - t0 < 25000) {
      main_ = app.windows().find((w) => w.url().includes('index.html')) || null;
      if (!main_) await sleep(200);
    }
    if (!main_) throw new Error('主窗口未出现');

    // ---------- 1. 欢迎页 ----------
    await main_.waitForSelector('text=欢迎使用实时字幕翻译', { timeout: 15000 });
    check('引导第1步：欢迎页呈现', true);

    // ---------- 2. 音频源页（真实 wsRequest 枚举） ----------
    await main_.getByText('开始设置', { exact: true }).click();
    await main_.waitForSelector('text=选择音频源', { timeout: 8000 });
    await main_.waitForFunction(
      () => Array.from(document.querySelectorAll('option'))
        .some((o) => (o.textContent || '').includes('假扬声器')),
      null,
      { timeout: 8000 }
    );
    check('引导第2步：音频源页 + 假后端设备枚举到达下拉', true);
    await main_.getByText('下一步', { exact: true }).click();

    // ---------- 3. 下载页：注入进度 → 进度条 → 100% 自动前进 ----------
    await main_.waitForSelector('text=模型准备', { timeout: 8000 });
    check('引导第3步：模型准备页呈现', true);

    send({
      type: 'model_progress', model_name: 'whisper-base',
      progress: 12, message: 'downloading 12%'
    });
    await main_.waitForSelector('text=whisper-base', { timeout: 8000 });
    const barPct = await main_.evaluate(() => {
      const bar = document.querySelector('[role="progressbar"] > div');
      return bar instanceof HTMLElement ? bar.style.width : '';
    });
    check('model_progress 驱动进度条(12%)', barPct === '12%', `width=${barPct}`);

    send({
      type: 'model_progress', model_name: 'whisper-base',
      progress: 100, message: 'complete'
    });
    // controller 在 100% 后 3s 清空 modelDownload → 页面 effect 自动前进
    await main_.waitForSelector('text=一切就绪', { timeout: 15000 });
    check('进度 100% 后自动前进至完成页', true);

    // ---------- 4. 进入主界面 + 旗标落库 ----------
    await main_.getByText('进入主界面', { exact: true }).click();
    await main_.waitForSelector('text=直播字幕', { timeout: 10000 });
    check('完成页 → 直播主界面', true);
    const cfg1 = await main_.evaluate(() => window.appAPI.getConfig());
    check('onboarding.completed 已持久化', cfg1.onboarding.completed === true);

    // ---------- 5. 跳过引导路径 ----------
    await main_.evaluate(() => window.appAPI.setConfig('onboarding.completed', false));
    await main_.evaluate(() => { window.location.hash = '#/onboarding'; });
    await main_.waitForSelector('text=欢迎使用实时字幕翻译', { timeout: 8000 });
    await main_.getByText('开始设置', { exact: true }).click();
    await main_.waitForSelector('text=跳过引导', { timeout: 8000 });
    await main_.getByText('跳过引导', { exact: true }).click();
    await main_.waitForSelector('text=直播字幕', { timeout: 8000 });
    const cfg2 = await main_.evaluate(() => window.appAPI.getConfig());
    check('跳过引导 → 直达直播 + 旗标置位', cfg2.onboarding.completed === true);

    // ---------- 6. Gateway 对齐旁证 ----------
    check('引导全程 Gateway config_sync 曾送达', received.some((m) => m.type === 'config_sync'));
  } catch (err) {
    check('ONBOARDING E2E 异常', false, String(err && err.message).split('\n')[0]);
  } finally {
    try { if (app) await app.close(); } catch { /* 已退出 */ }
    await sleep(800);
    try {
      fs.writeFileSync(STORE_PATH, storeBackup);
    } catch (e) {
      results.push(`FAIL | store 还原失败: ${e.message}`);
      exitCode = 1;
    }
    try { wss.close(); } catch { /* noop */ }
    clearTimeout(alarm);
    console.log(results.join('\n'));
    console.log(`E2E_ONBOARDING_DONE exitCode=${exitCode}`);
    process.exit(exitCode);
  }
}

main().catch((e) => {
  console.log('FATAL', e && e.stack);
  process.exit(2);
});
