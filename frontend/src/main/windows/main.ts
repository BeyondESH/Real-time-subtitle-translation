/**
 * 主窗口（main-window spec：主窗口壳与标题栏）
 *
 * - 无边框 + WCO（titleBarOverlay，原生窗口按钮）；不支持的系统降级系统边框并记日志
 * - 单例：showMainWindow 聚焦还原已有窗口
 * - 关闭 = 隐藏到托盘（应用继续运行）；仅托盘"退出"真正退出
 * - 位置尺寸持久化到 config ui.mainWindow；主题切换同步原生按钮配色
 */
import { BrowserWindow, nativeTheme } from 'electron';
import * as os from 'os';
import type { ConfigStore } from '../config';
import type { GatewayLogger } from '../types';
import { loadRenderer, preloadPath } from './util';

export const TITLEBAR_HEIGHT = 36;

export interface TitleBarColors {
  color: string;
  symbolColor: string;
}

/** 与 renderer token 对齐的标题栏配色（主进程侧允许色值字面量，色值扫描只约束渲染层） */
const DARK_BAR: TitleBarColors = { color: '#171717', symbolColor: '#ececec' };
const LIGHT_BAR: TitleBarColors = { color: '#f9f9f9', symbolColor: '#1a1a1a' };
const DARK_BG = '#0d0d0d';
const LIGHT_BG = '#ffffff';

function winBuildNumber(): number {
  if (process.platform !== 'win32') return 0;
  return Number(os.release().split('.')[2] ?? 0);
}

/** WCO：Windows 10 1809 (17763) 起支持 titleBarOverlay */
export function supportsWco(): boolean {
  return process.platform === 'win32' && winBuildNumber() >= 17763;
}

/** 亚克力材质：Windows 11 (22000) 起支持 backgroundMaterial */
export function supportsAcrylic(): boolean {
  return process.platform === 'win32' && winBuildNumber() >= 22000;
}

export function resolveIsDark(theme: 'dark' | 'light' | 'system'): boolean {
  return theme === 'system' ? nativeTheme.shouldUseDarkColors : theme === 'dark';
}

export function titleBarColors(theme: 'dark' | 'light' | 'system'): TitleBarColors {
  return resolveIsDark(theme) ? DARK_BAR : LIGHT_BAR;
}

export interface MainWindowDeps {
  config: ConfigStore;
  logger: GatewayLogger;
  /** 关闭请求：true=允许销毁（应用退出），false=拦截并隐藏 */
  isQuitting(): boolean;
}

export function createMainWindow(deps: MainWindowDeps): BrowserWindow {
  const ui = deps.config.get('ui');
  const saved = ui.mainWindow;
  const useWco = supportsWco();
  if (!useWco) {
    deps.logger.warn('系统不支持 WCO 标题栏，降级为系统边框');
  }

  const theme = deps.config.get('theme');
  const isDark = resolveIsDark(theme);

  const win = new BrowserWindow({
    width: saved.width,
    height: saved.height,
    x: saved.x ?? undefined,
    y: saved.y ?? undefined,
    minWidth: 760,
    minHeight: 480,
    show: false,
    frame: !useWco,
    titleBarStyle: useWco ? 'hidden' : undefined,
    titleBarOverlay: useWco
      ? { ...titleBarColors(theme), height: TITLEBAR_HEIGHT }
      : undefined,
    backgroundColor: isDark ? DARK_BG : LIGHT_BG,
    autoHideMenuBar: true,
    webPreferences: {
      preload: preloadPath(),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  loadRenderer(win, 'index');

  win.once('ready-to-show', () => {
    win.show();
  });

  // 关闭 = 隐藏到托盘；仅应用退出流程允许真正销毁
  win.on('close', (e) => {
    if (!deps.isQuitting()) {
      e.preventDefault();
      win.hide();
    }
  });

  const persistBounds = (): void => {
    if (win.isDestroyed()) return;
    const b = win.getBounds();
    deps.config.set('ui', {
      ...deps.config.get('ui'),
      mainWindow: { width: b.width, height: b.height, x: b.x, y: b.y }
    });
  };
  win.on('moved', persistBounds);
  win.on('resized', persistBounds);

  return win;
}

/** 主题变化时同步原生按钮配色（无 WCO 时静默跳过） */
export function applyTitleBarTheme(win: BrowserWindow, theme: 'dark' | 'light' | 'system'): void {
  if (win.isDestroyed() || !supportsWco()) return;
  try {
    win.setTitleBarOverlay({ ...titleBarColors(theme), height: TITLEBAR_HEIGHT });
    win.setBackgroundColor(resolveIsDark(theme) ? DARK_BG : LIGHT_BG);
  } catch (err) {
    // 个别 Windows 版本 setTitleBarOverlay 可能抛错，不影响功能
  }
}

/** 单例语义：显示并聚焦（最小化则还原） */
export function showMainWindow(win: BrowserWindow | null): BrowserWindow | null {
  if (!win || win.isDestroyed()) return null;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
  return win;
}
