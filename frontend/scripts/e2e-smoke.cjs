/**
 * E2E 冒烟（打包版真应用 + 假后端 WS）— 自动化验收 2.12 / 4.11 的可自动化子集
 *
 * 原理：临时把 store 的 websocket.port 指向本脚本的假后端（占住端口，
 * 应用自 spawn 的 python 因端口冲突退出、Gateway 连到假后端），
 * 从而在【打包产物】上以受控广播驱动全链路：
 *   Gateway 连接对齐 → 字幕注入 → Live 渲染 → SQLite 落库 → 搜索
 *   → 暂停链路(UI→IPC→WS帧) → 主题热切换 → 主窗口隐藏期间照常入库(4.11 专项)
 *   → vad_state/pipeline_warning 状态路由
 *
 * 用法：node scripts/e2e-smoke.cjs   （先 npm run dist 或已有 release/win-unpacked）
 * 副作用：临时改写 %APPDATA%\real-time-subtitle-translator\config.json（结束还原）；
 *         history.db 留下两条"测试字幕"（可在 设置-高级-清空全部历史 一并清掉）。
 */
const { _electron } = require('playwright-core');
const { WebSocketServer } = require('ws');
const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');

const FAKE_PORT = 18999;
const STORE_PATH = path.join(
  process.env.APPDATA, 'real-time-subtitle-translator', 'config.json'
);
const PACKAGED_EXE = process.env.E2E_EXE
  ? path.resolve(process.env.E2E_EXE)
  : path.resolve(__dirname, '..', 'release', 'win-unpacked', '实时字幕翻译.exe');

// 兜底：总超时强杀（异常挂起时保证 store 还原在 finally 里已注册）
const alarm = setTimeout(() => {
  console.log('E2E_TIMEOUT');
  process.exit(3);
}, 150000);

const results = [];
let exitCode = 0;
function check(name, ok, extra) {
  results.push(`${ok ? 'PASS' : 'FAIL'} | ${name}${extra ? ` (${extra})` : ''}`);
  if (!ok) exitCode = 1;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ARTIFACTS = path.resolve(__dirname, '..', 'e2e-artifacts');

/** 截图留档（4.1 双形态核对 / README 素材），失败不阻塞 */
async function capture(page, name) {
  try {
    fs.mkdirSync(ARTIFACTS, { recursive: true });
    await page.screenshot({ path: path.join(ARTIFACTS, name) });
    results.push(`INFO | 截图留档 e2e-artifacts/${name}`);
  } catch (e) {
    results.push(`INFO | 截图失败 ${name}: ${e.message}`);
  }
}

/**
 * Win32 WindowFromPoint + GetAncestor(GA_ROOT)（只读查询）。
 * 返回命中点的最深子窗口及其顶层根窗口：
 * Chromium 窗口是顶层宿主+内容子窗口族，WindowFromPoint 返回的是子级，
 * 归属判定必须用 GA_ROOT 与 Electron 顶层 HWND 比较。
 * 锁定态（WS_EX_TRANSPARENT）整族被命中测试跳过 → 根窗口=下层窗口。
 */
function hwndAt(x, y) {
  return new Promise((resolve, reject) => {
    const ps = [
      "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;",
      'public struct PT{public int X;public int Y;}',
      'public class W32{[DllImport("user32.dll")]public static extern IntPtr WindowFromPoint(PT p);',
      '[DllImport("user32.dll")]public static extern IntPtr GetAncestor(IntPtr h, uint f);}',
      "'",
      `$pt = New-Object PT; $pt.X=${Math.round(x)}; $pt.Y=${Math.round(y)}`,
      '$hit = [W32]::WindowFromPoint($pt)',
      '$root = [W32]::GetAncestor($hit, 2)',
      '"$($hit.ToInt64()) $($root.ToInt64())"'
    ].join('\n');
    execFile('powershell.exe', ['-NoProfile', '-Command', ps],
      { timeout: 45000, windowsHide: true },
      (err, stdout) => {
        if (err) return reject(err);
        const parts = stdout.trim().split(/\s+/).map(Number);
        resolve({ hit: parts[0], root: parts[1] });
      });
  });
}

async function waitFor(fn, timeout = 10000, what = '') {
  const t0 = Date.now();
  while (Date.now() - t0 < timeout) {
    if (await fn()) return true;
    await sleep(150);
  }
  throw new Error(`waitFor timeout: ${what}`);
}

async function main() {
  results.push(`DIAG | E2E进程 ELECTRON_RENDERER_URL=${process.env.ELECTRON_RENDERER_URL || '(unset)'}`);
  // ---------- 1. 假后端 ----------
  const received = [];
  let client = null;
  const wss = new WebSocketServer({ port: FAKE_PORT }); // 双栈，localhost(::1/127) 均可达
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
  const saw = (pred) => received.some(pred);

  // ---------- 2. store 端口临时改写（备份） ----------
  if (!fs.existsSync(STORE_PATH)) throw new Error(`store 不存在: ${STORE_PATH}`);
  const storeBackup = fs.readFileSync(STORE_PATH, 'utf8');
  const storeJson = JSON.parse(storeBackup);
  storeJson.websocket = { ...(storeJson.websocket || {}), host: 'localhost', port: FAKE_PORT };
  storeJson.onboarding = { completed: true };
  fs.writeFileSync(STORE_PATH, JSON.stringify(storeJson, null, 2));

  let app = null;
  try {
    // ---------- 3. 启动打包版（失败回退开发版） ----------
    let mode = 'packaged';
    try {
      app = await _electron.launch({ executablePath: PACKAGED_EXE });
    } catch (e) {
      mode = 'dev-fallback';
      const electronExe = require('electron'); // 返回 dev electron 路径
      app = await _electron.launch({
        executablePath: electronExe,
        args: [path.resolve(__dirname, '..')]
      });
    }
    check(`应用启动（${mode}）`, true);

    // ---------- 4. 双窗口（按 URL 轮询等待导航完成，而非抢先快照） ----------
    let main_ = null;
    let overlay = null;
    try {
      await waitFor(() => {
        const wins = app.windows();
        main_ = wins.find((w) => w.url().includes('index.html')) || null;
        overlay = wins.find((w) => w.url().includes('overlay.html')) || null;
        return Boolean(main_ && overlay);
      }, 25000, 'main+overlay windows by URL');
    } catch (e) {
      results.push(`DIAG | 窗口数=${app.windows().length} URLs=[${app.windows()
        .map((w) => w.url() || '(empty)').join(' , ')}]`);
      throw e;
    }
    check('主窗口 + 悬浮窗均创建', Boolean(main_ && overlay));

    // ---------- 5. Gateway 连接与偏好对齐 ----------
    await waitFor(() => client !== null, 15000, 'gateway connect');
    check('Gateway 连上假后端', true);
    await waitFor(() => saw((m) => m.type === 'config_sync'), 8000, 'config_sync');
    check('连接后自动 config_sync', true);
    await waitFor(
      () => saw((m) => m.type === 'request' && m.method === 'get_config'), 8000, 'get_config'
    );
    check('偏好对齐 get_config', true);

    // ---------- 6. Live 视图渲染 ----------
    await main_.waitForSelector('text=直播字幕', { timeout: 15000 });
    check('主窗口直播页渲染（无白屏）', true);
    await main_.waitForSelector('text=暂无字幕', { timeout: 8000 });
    check('空态渲染', true);

    // ---------- 7. 字幕注入 → 渲染 + 落库 + 搜索 ----------
    send({
      type: 'subtitle', original: 'テスト音声です', source_language: 'ja',
      active_language: 'zh', translations: { zh: '这是E2E测试字幕' },
      ts_start: 1.0, ts_end: 2.5
    });
    await main_.waitForSelector('text=这是E2E测试字幕', { timeout: 8000 });
    check('注入字幕实时渲染到直播流', true);
    await waitFor(async () => {
      const s = await main_.evaluate(() => window.appAPI.listSessions());
      return s.length > 0 && s[0].utterance_count >= 1;
    }, 8000, 'history count>=1');
    const sessions1 = await main_.evaluate(() => window.appAPI.listSessions());
    check('SQLite 真实落库', sessions1[0].utterance_count >= 1,
      `count=${sessions1[0].utterance_count}`);
    const title = sessions1[0].title;
    check('首句自动改题（译文前12字）', title.includes('这是E2E测试字幕'.slice(0, 8)),
      `title=${title}`);
    const hits = await main_.evaluate(() => window.appAPI.searchHistory('测试字幕'));
    check('全文搜索命中', hits.length >= 1, `hits=${hits.length}`);

    // ---------- 8. 暂停链路：UI dispatch → 主进程 → WS 帧 + 状态 ----------
    await main_.evaluate(() => window.appAPI.dispatch({ type: 'togglePause' }));
    await waitFor(() => saw((m) => m.type === 'control' && m.action === 'pause'), 5000, 'pause frame');
    const stPaused = await main_.evaluate(() => window.appAPI.getState());
    check('暂停全链路（IPC→WS帧→状态广播）', stPaused.capture === 'paused');
    await main_.evaluate(() => window.appAPI.dispatch({ type: 'togglePause' }));
    await waitFor(() => saw((m) => m.type === 'control' && m.action === 'resume'), 5000, 'resume frame');

    // ---------- 9. 主题热切换 + 运行时视觉断言（fix-tailwind-build-pipeline） ----------
    await main_.evaluate(() => window.appAPI.setConfig('theme', 'light'));
    await main_.waitForFunction(
      () => document.documentElement.dataset.theme === 'light', null, { timeout: 5000 }
    );
    check('主题切换实时生效（config→广播→DOM）', true);

    // 视觉断言：bg-base 根容器计算背景色 = 设计 token（CSS 管线真实生效的证据；
    // 样式断链时该类无规则 → 背景为 rgba(0, 0, 0, 0)，此处必然失败）
    await sleep(200);
    const bgLight = await main_.evaluate(() => {
      const el = document.querySelector('.bg-base');
      return el ? getComputedStyle(el).backgroundColor : null;
    });
    check('视觉断言：亮色根容器背景 = rgb(255, 255, 255)', bgLight === 'rgb(255, 255, 255)',
      `bg=${bgLight}`);
    await capture(main_, 'main-light.png');

    await main_.evaluate(() => window.appAPI.setConfig('theme', 'dark'));
    await main_.waitForFunction(
      () => document.documentElement.dataset.theme !== 'light', null, { timeout: 5000 }
    );
    await sleep(200);
    const bgDark = await main_.evaluate(() => {
      const el = document.querySelector('.bg-base');
      return el ? getComputedStyle(el).backgroundColor : null;
    });
    check('视觉断言：暗色根容器背景 = rgb(13, 13, 13)', bgDark === 'rgb(13, 13, 13)',
      `bg=${bgDark}`);
    await capture(main_, 'main-dark.png');

    // ---------- 9b. 会话回放页 ----------
    const sid = (await main_.evaluate(() => window.appAPI.listSessions()))[0].id;
    await main_.evaluate((id) => { window.location.hash = `#/session/${id}`; }, sid);
    await main_.waitForSelector('text=这是E2E测试字幕', { timeout: 8000 });
    await main_.waitForSelector('text=导出', { timeout: 5000 });
    check('会话回放页渲染（语句 + 导出入口）', true);

    // ---------- 9c. 深链导航（与托盘"设置"同通道 app:navigate） ----------
    await app.evaluate(async ({ BrowserWindow }) => {
      const wins = BrowserWindow.getAllWindows();
      const mw = wins.find((w) => w.webContents.getURL().includes('index.html'));
      if (mw) mw.webContents.send('app:navigate', '/settings/shortcuts');
    });
    await main_.waitForSelector('text=暂停/恢复', { timeout: 8000 });
    await main_.waitForSelector('text=恢复默认', { timeout: 5000 });
    check('主进程深链导航 → 设置/快捷键分段', true);

    // 回到直播页（后续经 overlay 断言，主页面即将因 close 模拟而失效）
    await main_.evaluate(() => { window.location.hash = '#/live'; });
    await main_.waitForSelector('text=直播字幕', { timeout: 8000 });

    // ---------- 10. 主窗口隐藏期间照常入库（4.11 专项） ----------
    // 用主进程 BrowserWindow.close() 模拟原生 ✕（与 DOM window.close() 不同事件链：
    // 前者走 'close' 事件可被 preventDefault 拦截 → hide；后者是 Chromium 脚本关窗）
    const hideInfo = await app.evaluate(({ BrowserWindow }) => {
      const mw = BrowserWindow.getAllWindows()
        .find((w) => w.webContents.getURL().includes('index.html'));
      if (!mw) return { found: false };
      mw.close();
      return {
        found: true,
        count: BrowserWindow.getAllWindows().length,
        visible: mw.isVisible(),
        destroyed: mw.isDestroyed()
      };
    });
    check(
      '原生关闭路径=隐藏（窗口保留且不可见）',
      hideInfo.found && !hideInfo.destroyed && hideInfo.count === 2 && hideInfo.visible === false,
      JSON.stringify(hideInfo)
    );
    await sleep(400);

    send({
      type: 'subtitle', original: '非表示中のテスト', source_language: 'ja',
      active_language: 'zh', translations: { zh: '隐藏窗期间第二条' },
      ts_start: 5.0, ts_end: 6.0
    });
    await waitFor(async () => {
      const s = await overlay.evaluate(() => window.appAPI.listSessions());
      return s.length > 0 && s[0].utterance_count >= 2;
    }, 8000, 'hidden-window history count>=2');
    const sessions2 = await overlay.evaluate(() => window.appAPI.listSessions());
    check('主窗口隐藏期间字幕照常入库（4.11 专项）', sessions2[0].utterance_count >= 2,
      `count=${sessions2[0].utterance_count}`);

    // ---------- 11. 悬浮窗扇出 + 渲染 ----------
    const overlayText = await overlay.evaluate(() => document.body.innerText);
    check('悬浮窗同步收到字幕（IPC 扇出双窗口）', overlayText.includes('隐藏窗期间第二条'));
    await capture(overlay, 'overlay-subtitle.png');

    // ---------- 12. vad_state / pipeline_warning 状态路由 ----------
    send({ type: 'vad_state', state: 'speech' });
    send({ type: 'pipeline_warning', reason: 'queue_full', dropped: 2, message: 'E2E过载' });
    await waitFor(async () => {
      const s = await overlay.evaluate(() => window.appAPI.getState());
      return s.vad === 'speech' && s.droppedCount === 2;
    }, 8000, 'vad+warning state');
    const st2 = await overlay.evaluate(() => window.appAPI.getState());
    check('vad_state/pipeline_warning → AppState', st2.vad === 'speech' && st2.droppedCount === 2);

    // ---------- 13. 高级页数据源 IPC（经存活的悬浮窗页面） ----------
    const stats = await overlay.evaluate(() => window.appAPI.historyStats());
    check('historyStats IPC（高级页数据源）', stats && stats.utteranceCount >= 2,
      `utterances=${stats && stats.utteranceCount}`);

    // ---------- 13b. 导出链路（主进程内 patch 原生保存对话框，验证文件真实落盘与格式） ----------
    const exportPath = path.join(require('os').tmpdir(), `e2e-export-${Date.now()}.srt`);
    await app.evaluate(async ({ dialog }, filePath) => {
      // index.ts 处理器在调用时解析 dialog.showSaveDialog → 本 patch 生效
      dialog.showSaveDialog = async () => ({ canceled: false, filePath });
    }, exportPath);
    const activeSid = await overlay.evaluate(async () => {
      const st = await window.appAPI.getState();
      return st.activeSessionId;
    });
    const exportRes = await overlay.evaluate(
      async (id) => window.appAPI.exportSession(id, 'srt'), activeSid
    );
    check('exportSession 返回 ok（IPC→导出器→fs 写盘）',
      exportRes && exportRes.ok === true, JSON.stringify(exportRes));
    if (exportRes && exportRes.ok && fs.existsSync(exportPath)) {
      const srt = fs.readFileSync(exportPath, 'utf8');
      const srtOk = /\d+\r?\n\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}\r?\n.+/.test(srt)
        && srt.includes('这是E2E测试字幕');
      // 注入时带 ts_start/ts_end → 精确时间轴（非近似）
      check('SRT 文件落盘：格式合法 + 内容命中 + 精确时间轴',
        srtOk && exportRes.approximate === false,
        `approximate=${exportRes.approximate} bytes=${srt.length}`);
      try { fs.unlinkSync(exportPath); } catch { /* noop */ }
    } else {
      check('SRT 文件落盘', false, `文件不存在: ${exportPath}`);
    }

    // ---------- 13c. 锁定穿透语义（Win32 命中测试实证，overlay-window spec） ----------
    const stBefore = await overlay.evaluate(() => window.appAPI.getState());
    const wasLocked = stBefore.locked;
    if (!wasLocked) {
      await overlay.evaluate(() => window.appAPI.dispatch({ type: 'toggleLock' }));
      await sleep(400);
    }
    const ow = await app.evaluate(({ BrowserWindow }) => {
      const w = BrowserWindow.getAllWindows()
        .find((x) => x.webContents.getURL().includes('overlay.html'));
      if (!w) return null;
      const b = w.getBounds();
      return {
        x: b.x, y: b.y, width: b.width, height: b.height,
        hwnd: w.getNativeWindowHandle().readInt32LE(0)
      };
    });
    if (ow) {
      const cx = ow.x + Math.floor(ow.width / 2);
      const cy = ow.y + Math.floor(ow.height / 2);
      const lockedHit = await hwndAt(cx, cy);
      check('锁定态：命中测试穿透（GA_ROOT 非悬浮窗）',
        Number.isFinite(lockedHit.root) && lockedHit.root !== ow.hwnd,
        `overlayHwnd=${ow.hwnd} hitRoot=${lockedHit.root}`);

      await overlay.evaluate(() => window.appAPI.dispatch({ type: 'toggleLock' }));
      await sleep(400);
      const unlockedHit = await hwndAt(cx, cy);
      check('解锁态：命中测试归属悬浮窗族（GA_ROOT 相等，可交互）',
        unlockedHit.root === ow.hwnd,
        `overlayHwnd=${ow.hwnd} hit=${unlockedHit.hit} hitRoot=${unlockedHit.root}`);

      // 恢复原锁定状态
      if (wasLocked) {
        await overlay.evaluate(() => window.appAPI.dispatch({ type: 'toggleLock' }));
        await sleep(300);
      }
    } else {
      check('锁定穿透命中测试（未找到悬浮窗）', false);
    }

    // ---------- 14. 清理测试数据（同时实测 clearAll 全链路） ----------
    const cleared = await overlay.evaluate(() => window.appAPI.clearHistory());
    check('清空全部历史（clearAll 链路 + 测试数据清理）',
      cleared && cleared.utteranceCount === 0 && cleared.sessionCount === 1,
      JSON.stringify(cleared));
  } catch (err) {
    check('E2E 执行异常', false, String(err && err.message).split('\n')[0]);
  } finally {
    // ---------- 清理：关应用 → 还原 store → 关假后端 ----------
    try { if (app) await app.close(); } catch { /* 已退出 */ }
    await sleep(800);
    try { fs.writeFileSync(STORE_PATH, storeBackup); } catch (e) {
      results.push(`FAIL | store 还原失败: ${e.message}`);
      exitCode = 1;
    }
    try { wss.close(); } catch { /* noop */ }
    clearTimeout(alarm);
    console.log(results.join('\n'));
    console.log(`E2E_DONE exitCode=${exitCode}`);
    process.exit(exitCode);
  }
}

main().catch((e) => {
  console.log('FATAL', e && e.stack);
  process.exit(2);
});
