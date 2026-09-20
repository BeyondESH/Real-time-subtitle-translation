/**
 * Electron 主进程入口
 *
 * 架构（client-gateway-state spec）：
 *   ConfigStore（偏好唯一真相） + BackendManager（进程托管） + Gateway（唯一 WS）
 *   + StateStore/AppState（唯一状态机） + Controller（意图执行/广播路由）
 * 窗口无状态：主窗口/overlay/settings 只是显示器，托盘与快捷键只是触发端，
 * 一切动作经 Controller.handle(intent) 单一入口。
 *
 * P1 新增：WCO 主窗口（关闭=隐藏托盘、深链导航、主题同步原生按钮配色）、
 *         app:getEnv / app:setConfig（白名单+副作用路由）/ app:openPath、开机自启。
 */
import {
  app, BrowserWindow, ipcMain, screen, shell, Tray, Menu, globalShortcut, dialog, nativeImage
} from 'electron';
import * as fs from 'fs';
import * as path from 'path';
import log from 'electron-log/main';
import iconPngPath from '../../resources/icon.png?asset';

import { ConfigStore } from './config';
import { audioSourceLabel } from './config-migration';
import { BackendManager } from './backend-manager';
import { Gateway, GatewayError } from './gateway';
import { createInitialState, StateStore } from './state';
import { AlignmentTracker } from './prefs-align';
import { Controller } from './controller';
import { HistoryStore } from './history-db';
import { exportSession, fileExtension, type ExportFormat } from './exporters';
import { UpdaterService } from './updater';
import {
  createMainWindow, showMainWindow, applyTitleBarTheme, supportsWco, supportsAcrylic
} from './windows/main';
import { loadRenderer, preloadPath } from './windows/util';
import type { DisplayInfo, EnvInfo, Intent, WsResponse } from '../shared/ipc-types';
import { ENGINE_MODEL_DISPLAY, WRITABLE_CONFIG_PATHS } from '../shared/ipc-types';

// ---------- 模块级组装引用（whenReady 中初始化） ----------

let config: ConfigStore | null = null;
let state: StateStore | null = null;
let gateway: Gateway | null = null;
let backendManager: BackendManager | null = null;
let controller: Controller | null = null;
let history: HistoryStore | null = null;
let updater: UpdaterService | null = null;

let mainWindow: BrowserWindow | null = null;
let overlayWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let quitting = false;
let persistTimer: ReturnType<typeof setTimeout> | null = null;

const logBridge = {
  info: (...args: unknown[]) => log.info(...args),
  warn: (...args: unknown[]) => log.warn(...args),
  error: (...args: unknown[]) => log.error(...args)
};

// ---------- 通用 ----------

/** 向所有存活窗口广播 IPC 事件 */
function broadcast(channel: string, payload: unknown): void {
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) {
      win.webContents.send(channel, payload);
    }
  }
}

function broadcastConfig(): void {
  broadcast('app:configChanged', config!.all);
}

// ---------- 字幕悬浮窗 ----------

/**
 * 多显示器放置（P3 task 5.2）：
 * 选定显示器（或主显示器）→ 该显示器记忆位置 → 越界自动回收进可视区
 */
function resolveOverlayPlacement(): { x: number; y: number } {
  const w = config!.get('window');
  const displays = screen.getAllDisplays();
  const target = (w.displayId != null
    ? displays.find((d) => d.id === w.displayId)
    : undefined) ?? screen.getPrimaryDisplay();
  const wa = target.workArea;

  const saved = (w.positions ?? {})[String(target.id)]
    ?? (w.x !== null && w.y !== null ? { x: w.x, y: w.y } : null);
  const inside = saved !== null
    && saved.x >= wa.x - 40
    && saved.x <= wa.x + wa.width - 120
    && saved.y >= wa.y - 40
    && saved.y <= wa.y + wa.height - 80;
  if (saved && inside) {
    return { x: saved.x, y: saved.y };
  }
  return {
    x: wa.x + Math.floor((wa.width - w.width) / 2),
    y: wa.y + wa.height - w.height - 50
  };
}

/** 防抖持久化拖动/缩放结果（含所在显示器与按显示器位置记忆） */
function schedulePersistBounds(): void {
  if (persistTimer) clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    persistTimer = null;
    if (!overlayWindow || overlayWindow.isDestroyed() || !config) return;
    const b = overlayWindow.getBounds();
    const disp = screen.getDisplayMatching(b);
    const w = config.get('window');
    config.set('window', {
      ...w,
      x: b.x,
      y: b.y,
      width: b.width,
      height: b.height,
      displayId: disp.id,
      positions: { ...(w.positions ?? {}), [String(disp.id)]: { x: b.x, y: b.y } }
    });
  }, 400);
}

/** 显示器切换（设置页）后立即把悬浮窗移到目标显示器 */
function repositionOverlay(): void {
  if (!overlayWindow || overlayWindow.isDestroyed()) return;
  const { x, y } = resolveOverlayPlacement();
  const w = config!.get('window');
  overlayWindow.setBounds({ x, y, width: w.width, height: w.height });
}

function createOverlayWindow(): void {
  const windowConfig = config!.get('window');
  const preset = config!.get('subtitle').preset;
  const useAcrylic = preset === 'acrylic' && supportsAcrylic();
  const { x, y } = resolveOverlayPlacement();

  overlayWindow = new BrowserWindow({
    width: windowConfig.width,
    height: windowConfig.height,
    x,
    y,
    // 毛玻璃胶囊预设：Win11 系统亚克力材质（窗口不透明，页面保持透明背景让材质透出）
    transparent: !useAcrylic,
    ...(useAcrylic ? { backgroundMaterial: 'acrylic' as const } : {}),
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: true,
    hasShadow: false,
    webPreferences: {
      preload: preloadPath(),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  overlayWindow.setOpacity(windowConfig.opacity);
  applyLockMode(state!.getState().locked);
  loadRenderer(overlayWindow, 'overlay');

  if (!state!.getState().overlayVisible) {
    overlayWindow.hide();
  }

  overlayWindow.on('closed', () => {
    overlayWindow = null;
  });

  overlayWindow.on('moved', schedulePersistBounds);
  overlayWindow.on('resized', schedulePersistBounds);
}

/** 预设切换需重建窗口（transparent 运行时不可切换，overlay-window spec） */
function recreateOverlayWindow(): void {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.removeAllListeners('closed');
    overlayWindow.destroy();
    overlayWindow = null;
  }
  createOverlayWindow();
}

/** 锁定=鼠标穿透纯展示；解锁=可拖拽可交互 */
function applyLockMode(locked: boolean): void {
  overlayWindow?.setIgnoreMouseEvents(locked, { forward: true });
}

// ---------- 主窗口 ----------

function ensureMainWindow(): BrowserWindow {
  if (mainWindow && !mainWindow.isDestroyed()) return mainWindow;
  mainWindow = createMainWindow({
    config: config!,
    logger: logBridge,
    isQuitting: () => quitting
  });
  mainWindow.on('closed', () => {
    mainWindow = null;
  });
  return mainWindow;
}

/** 显示主窗口并深链导航（托盘"设置"等入口） */
function showMainAndNavigate(route?: string): void {
  const win = ensureMainWindow();
  showMainWindow(win);
  if (route) {
    win.webContents.send('app:navigate', route);
  }
}

// ---------- 开机自启 ----------

function applyAutoStart(wanted: boolean): boolean {
  try {
    app.setLoginItemSettings({ openAtLogin: wanted });
    const actual = app.getLoginItemSettings().openAtLogin === true;
    if (actual !== wanted) {
      log.warn(`开机自启设置未生效: 期望 ${wanted} 实际 ${actual}`);
    }
    return actual;
  } catch (err) {
    log.error('开机自启设置失败:', err);
    try {
      return app.getLoginItemSettings().openAtLogin === true;
    } catch {
      return false;
    }
  }
}

/** 启动时以系统登录项真实状态校准 store（settings-management spec） */
function calibrateAutoStart(): void {
  const saved = config!.get('system').autoStart;
  let actual = false;
  try {
    actual = app.getLoginItemSettings().openAtLogin === true;
  } catch (err) {
    log.warn('读取登录项状态失败:', err);
    return;
  }
  if (actual !== saved) {
    log.info(`开机自启状态校准: store=${saved} → 系统=${actual}`);
    config!.set('system', { autoStart: actual });
  }
}

// ---------- 托盘 ----------

function loadTrayIcon(): Electron.NativeImage {
  try {
    const img = nativeImage.createFromPath(iconPngPath);
    if (!img.isEmpty()) return img;
  } catch (err) {
    log.warn('托盘图标加载失败:', err);
  }
  log.warn('托盘图标不可用，降级为空图标');
  return nativeImage.createEmpty();
}

function createTray(): void {
  try {
    tray = new Tray(loadTrayIcon());
  } catch (err) {
    // 托盘创建失败不阻断应用（overlay-window spec）
    log.error('托盘创建失败:', err);
    return;
  }

  tray.setToolTip('实时字幕翻译');
  updateTrayMenu();

  tray.on('click', () => {
    controller?.handle({ type: 'toggleOverlay' });
  });
}

function updateTrayMenu(): void {
  if (!tray || !state) return;
  const s = state.getState();

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示主窗口',
      click: () => showMainAndNavigate()
    },
    {
      label: s.overlayVisible ? '隐藏字幕' : '显示字幕',
      click: () => controller?.handle({ type: 'toggleOverlay' })
    },
    {
      label: s.capture === 'paused' ? '恢复' : '暂停',
      click: () => controller?.handle({ type: 'togglePause' })
    },
    {
      label: '锁定字幕位置',
      type: 'checkbox',
      checked: s.locked,
      click: () => controller?.handle({ type: 'toggleLock' })
    },
    {
      label: '重启后端服务',
      click: () => controller?.handle({ type: 'restartBackend' })
    },
    {
      label: '设置',
      click: () => controller?.handle({ type: 'showSettings' })
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => app.quit()
    }
  ]);
  tray.setContextMenu(contextMenu);
}

// ---------- 全局快捷键 ----------

function tryRegisterShortcut(accelerator: string, intent: Intent): boolean {
  try {
    const ok = globalShortcut.register(accelerator, () => {
      controller?.handle(intent);
    });
    if (!ok) {
      log.error(`快捷键注册失败（可能被占用）: ${accelerator}`);
    }
    return ok;
  } catch (err) {
    log.error(`快捷键注册异常: ${accelerator}`, err);
    return false;
  }
}

function registerShortcuts(): void {
  const sc = config!.get('shortcuts');
  const status = {
    togglePause: tryRegisterShortcut(sc.togglePause, { type: 'togglePause' }),
    switchLanguage: tryRegisterShortcut(sc.switchLanguage, { type: 'cycleLanguage' }),
    toggleLock: tryRegisterShortcut(sc.toggleLock, { type: 'toggleLock' })
  };
  // 注册结果持久化，设置页警示展示（main-window spec）
  config!.set('shortcutStatus', status);
}

// ---------- IPC ----------

const KNOWN_INTENTS = new Set<string>([
  'togglePause', 'cycleLanguage', 'setLanguage', 'setSourceLanguage',
  'setLlm', 'toggleLock', 'toggleOverlay', 'setAudioSource', 'setDevice',
  'newSession', 'showSettings', 'restartBackend'
]);

function registerIpc(): void {
  ipcMain.handle('app:getState', () => state!.getState());
  ipcMain.handle('app:getConfig', () => config!.all);

  ipcMain.handle('app:intent', (_event, intent: Intent) => {
    if (typeof intent !== 'object' || intent === null || !KNOWN_INTENTS.has(intent.type)) {
      log.warn('忽略非法 intent:', intent);
      return;
    }
    controller!.handle(intent);
  });

  ipcMain.handle(
    'app:wsRequest',
    async (_event, method: string, params?: unknown): Promise<WsResponse> => {
      try {
        const result = await gateway!.request(method, params);
        return { ok: true, result };
      } catch (err) {
        if (err instanceof GatewayError) {
          return { ok: false, error: err.code, message: err.message };
        }
        return { ok: false, error: 'unknown', message: String(err) };
      }
    }
  );

  ipcMain.handle('app:getEnv', (): EnvInfo => ({
    platform: process.platform,
    supportsWco: supportsWco(),
    supportsAcrylic: supportsAcrylic(),
    version: app.getVersion()
  }));

  ipcMain.handle('app:getDisplays', (): DisplayInfo[] => {
    const primary = screen.getPrimaryDisplay();
    return screen.getAllDisplays().map((d, i) => ({
      id: d.id,
      label: `显示器 ${i + 1}${d.id === primary.id ? '（主）' : ''} · ${d.bounds.width}×${d.bounds.height}`,
      primary: d.id === primary.id,
      workArea: {
        x: d.workArea.x, y: d.workArea.y, width: d.workArea.width, height: d.workArea.height
      }
    }));
  });

  ipcMain.handle('app:checkUpdate', async () => (updater
    ? updater.check(false)
    : { phase: 'error' as const, error: 'updater unavailable' }));

  ipcMain.handle('app:quitAndInstall', () => {
    quitting = true;
    updater?.quitAndInstall();
  });

  // 配置写入：白名单校验 → 落盘 → 副作用路由 → 广播
  ipcMain.handle('app:setConfig', (_event, cfgPath: string, value: unknown): boolean => {
    if (typeof cfgPath !== 'string' || !WRITABLE_CONFIG_PATHS.includes(cfgPath)) {
      log.warn('拒绝非法配置路径:', cfgPath);
      return false;
    }

    if (cfgPath === 'system.autoStart') {
      const wanted = Boolean(value);
      const actual = applyAutoStart(wanted);
      config!.set('system', { autoStart: actual });
      if (actual !== wanted) {
        controller!.toast('开机自启设置失败，已按系统实际状态回退', 'error');
      }
      broadcastConfig();
      return true;
    }

    if (cfgPath === 'onboarding.completed') {
      config!.set('onboarding', { completed: Boolean(value) });
      broadcastConfig();
      return true;
    }

    config!.setByPath(cfgPath, value);

    // 副作用路由
    if (cfgPath === 'theme' && mainWindow && !mainWindow.isDestroyed()) {
      applyTitleBarTheme(mainWindow, config!.get('theme'));
      broadcastConfig();
      return true;
    }
    if (cfgPath === 'subtitle.preset') {
      recreateOverlayWindow();
      broadcastConfig();
      return true;
    }
    if (cfgPath === 'window.opacity') {
      overlayWindow?.setOpacity(Number(value));
      broadcastConfig();
      return true;
    }
    if (cfgPath === 'window.displayId') {
      repositionOverlay();
      broadcastConfig();
      return true;
    }
    if (
      cfgPath.startsWith('translation')
      || cfgPath === 'asr.language'
      || cfgPath === 'inference.device'
      || cfgPath === 'audio.source'
    ) {
      controller!.onConfigSaved();
    }

    broadcastConfig();
    return true;
  });

  ipcMain.handle('app:openPath', async (_event, kind: string): Promise<boolean> => {
    const userData = app.getPath('userData');
    const target = kind === 'logDir' ? path.join(userData, 'logs') : userData;
    try {
      fs.mkdirSync(target, { recursive: true });
      const result = await shell.openPath(target);
      return result === '';
    } catch (err) {
      log.error('打开目录失败:', target, err);
      return false;
    }
  });

  // ---------- 会话历史（session-history spec） ----------

  ipcMain.handle('history:listSessions', () => history!.listSessions());

  ipcMain.handle('history:getSession', (_event, id: string) => {
    const session = history!.getSession(id);
    if (!session) return null;
    return { session, utterances: history!.listUtterances(id) };
  });

  ipcMain.handle('history:search', (_event, query: string) => {
    if (typeof query !== 'string') return [];
    return history!.search(query);
  });

  ipcMain.handle('history:rename', (_event, id: string, title: string) => {
    const ok = history!.renameSession(id, String(title ?? ''));
    if (ok) broadcast('history:changed', { kind: 'session' });
    return ok;
  });

  ipcMain.handle('history:delete', (_event, id: string) => {
    const r = history!.deleteSession(String(id));
    if (r.wasActive) {
      // 删除活跃会话 → 自动开启新会话（spec 场景）
      const nid = history!.newSession(
        Date.now(), audioSourceLabel(config!.get('audio').source)
      );
      state!.dispatch({ type: 'activeSessionChanged', id: nid });
    }
    broadcast('history:changed', { kind: 'session' });
    return r;
  });

  ipcMain.handle('history:newSession', () => {
    controller!.handle({ type: 'newSession' });
    return state!.getState().activeSessionId;
  });

  ipcMain.handle('history:stats', () => history!.stats());

  ipcMain.handle('history:clear', () => {
    history!.clearAll(Date.now(), audioSourceLabel(config!.get('audio').source));
    state!.dispatch({ type: 'activeSessionChanged', id: history!.getActiveId() });
    broadcast('history:changed', { kind: 'clear' });
    return history!.stats();
  });

  ipcMain.handle(
    'history:export',
    async (
      _event, sessionId: string, format: string
    ): Promise<{ ok: boolean; filePath?: string; error?: string; approximate?: boolean }> => {
      const formats: ExportFormat[] = ['srt', 'txt', 'md', 'json'];
      if (!formats.includes(format as ExportFormat)) {
        return { ok: false, error: `未知导出格式: ${format}` };
      }
      const data = history!.exportData(String(sessionId));
      if (!data) return { ok: false, error: '会话不存在' };

      const safeTitle = data.title.replace(/[\\/:*?"<>|]/g, '_').slice(0, 60) || 'session';
      const ext = fileExtension(format as ExportFormat);
      const options: Electron.SaveDialogOptions = {
        defaultPath: `${safeTitle}.${ext}`,
        filters: [{ name: ext.toUpperCase(), extensions: [ext] }]
      };
      const anchor = BrowserWindow.getFocusedWindow() ?? mainWindow;
      const result = anchor
        ? await dialog.showSaveDialog(anchor, options)
        : await dialog.showSaveDialog(options);
      if (result.canceled || !result.filePath) {
        return { ok: false, error: 'canceled' };
      }
      try {
        const exported = exportSession(data, format as ExportFormat);
        fs.writeFileSync(result.filePath, exported.content, 'utf8');
        return { ok: true, filePath: result.filePath, approximate: exported.approximateTimeline };
      } catch (err) {
        log.error('导出失败:', err);
        return { ok: false, error: String(err) };
      }
    }
  );
}

// ---------- 状态订阅：窗口效果 + 托盘同步 ----------

function subscribeStateEffects(): void {
  state!.subscribe((patch) => {
    broadcast('app:statePatch', patch);

    if ('locked' in patch) {
      applyLockMode(state!.getState().locked);
    }
    if ('overlayVisible' in patch && overlayWindow && !overlayWindow.isDestroyed()) {
      if (state!.getState().overlayVisible) {
        overlayWindow.show();
      } else {
        overlayWindow.hide();
      }
    }
    if ('capture' in patch || 'locked' in patch || 'overlayVisible' in patch) {
      updateTrayMenu();
    }
  });
}

// ---------- 后端托管 ----------

async function restartBackendFlow(): Promise<void> {
  controller!.notifyBackendRestarted();
  await backendManager!.restart();
}

/** 轮询端口直到后端就绪；超时弹窗允许重试或退出（release-packaging spec） */
async function ensureBackendHealthy(): Promise<void> {
  const ok = await backendManager!.waitForReady();
  if (ok) return;

  const logPath = path.join(app.getPath('userData'), 'logs');
  const choice = await dialog.showMessageBox({
    type: 'error',
    title: '后端连接失败',
    message: '无法连接字幕后端服务',
    detail: `后端在 90 秒内未就绪。\n日志目录: ${logPath}`,
    buttons: ['重试', '退出']
  });
  if (choice.response === 0) {
    await restartBackendFlow();
    await ensureBackendHealthy();
  } else {
    app.quit();
  }
}

// ---------- 生命周期 ----------

app.whenReady().then(() => {
  log.initialize();
  log.transports.file.resolvePathFn = () =>
    path.join(app.getPath('userData'), 'logs', 'frontend.log');
  log.info('应用启动');

  // 1. 配置（偏好唯一真相）+ 旧 yaml 一次性迁移 + 自启校准
  config = new ConfigStore();
  config.migrateLegacyYaml(path.join(app.getPath('userData'), 'config.yaml'), logBridge);
  calibrateAutoStart();

  // 2. 历史库（先于状态机：初始状态需要活跃会话 id）
  history = new HistoryStore(
    path.join(app.getPath('userData'), 'history.db'), logBridge
  );
  const activeSessionId = history.ensureActiveSession(
    Date.now(), audioSourceLabel(config.get('audio').source)
  );

  // 3. 状态机（初值来自配置）
  const wsConfig = config.get('websocket');
  state = new StateStore(createInitialState({
    model: ENGINE_MODEL_DISPLAY,
    activeLanguage: config.get('translation').activeLanguage,
    targetLanguages: config.get('translation').targetLanguages,
    audioSource: audioSourceLabel(config.get('audio').source),
    locked: config.get('locked'),
    overlayVisible: true,
    activeSessionId
  }));

  // 4. 后端托管 + 唯一 WS Gateway
  backendManager = new BackendManager(
    {
      isPackaged: app.isPackaged,
      resourcesPath: process.resourcesPath,
      devBackendDir: path.join(app.getAppPath(), '..', 'backend'),
      devConfigTemplate: path.join(app.getAppPath(), '..', 'config.yaml'),
      userDataDir: app.getPath('userData')
    },
    { host: wsConfig.host, port: wsConfig.port },
    logBridge,
    {
      // 退出日志由 BackendManager 记录；此处只负责用户可见提示
      onExit: () => {
        tray?.displayBalloon({
          title: '实时字幕翻译',
          content: '后端服务已退出，可在托盘菜单重启'
        });
      }
    },
    {
      // 显式推理设备偏好经 SUBTITLE_DEVICE 注入；auto 不注入
      getDevice: () => config!.get('inference').device
    }
  );
  gateway = new Gateway({
    url: `ws://${wsConfig.host}:${wsConfig.port}`,
    logger: logBridge
  });
  const tracker = new AlignmentTracker();

  // 5. Controller：意图执行 + 广播路由 + 历史落库（窗口/托盘/快捷键的唯一动作源）
  controller = new Controller({
    gateway,
    state,
    config,
    history,
    tracker,
    logger: logBridge,
    broadcast: (channel, payload) => broadcast(channel, payload),
    onShowSettings: () => showMainAndNavigate('/settings/general'),
    onRestartBackend: () => {
      void restartBackendFlow().then(() => ensureBackendHealthy());
    }
  });
  backendManager.start();

  // 6. IPC + 状态效果订阅
  registerIpc();
  subscribeStateEffects();

  // 快捷键自定义：shortcuts 配置变化 → 全量重注册（注册结果写回 shortcutStatus）
  config.onDidChange('shortcuts', () => {
    globalShortcut.unregisterAll();
    registerShortcuts();
    broadcastConfig();
  });

  // 推理设备偏好变化 → 广播配置（设置页 Select 值取自 cfg.inference.device）
  config.onDidChange('inference', () => broadcastConfig());

  // 音频源偏好变化 → 广播配置（设置页 Select 值与胶囊面板选中态取自 cfg.audio.source；
  // 用户切换 / 退出回退 / 对齐重置均由 controller 直写 store，不广播则 UI 选中态不刷新）
  config.onDidChange('audio', () => broadcastConfig());

  // 翻译模型偏好变化 → 广播配置（设置页选择与失败回退写 store 后，UI 选中态需刷新；
  // 与后端实际运行态的对齐经 change_llm 由 controller/prefs-align 下发）
  config.onDidChange('translation', () => broadcastConfig());

  // 7. 自动更新（打包构建才启用；启动 15s 后静默检查）
  updater = new UpdaterService({
    logger: logBridge,
    broadcast: (event) => broadcast('app:update', event),
    onDownloaded: () => {
      tray?.displayBalloon({
        title: '实时字幕翻译',
        content: '新版本已下载完成：可在 设置-通用 立即重启安装，或退出应用时自动安装'
      });
    }
  });
  updater.init();

  // 8. 窗口 / 托盘 / 快捷键 / 连接 / 健康检查
  createOverlayWindow();
  ensureMainWindow();
  createTray();
  registerShortcuts();
  gateway.connect();
  void ensureBackendHealthy();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createOverlayWindow();
      ensureMainWindow();
    }
  });
});

app.on('before-quit', () => {
  quitting = true;
});

app.on('window-all-closed', () => {
  globalShortcut.unregisterAll();
  app.quit();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
  if (persistTimer) {
    clearTimeout(persistTimer);
    persistTimer = null;
  }
  controller?.dispose();
  updater?.dispose();
  gateway?.close();
  backendManager?.stop();
  // 会话封口（ended_at）后关闭数据库
  history?.closeActiveSession(Date.now());
  history?.dispose();
});
