/**
 * UpdaterService — 自动更新（release-packaging spec "自动更新"）
 *
 * - electron-updater + GitHub Releases（electron-builder.yml publish 段配置）
 * - 启动延迟静默检查；手动检查经 IPC；下载进度广播到主窗口状态区
 * - 下载完成：托盘通知"重启安装"（可推迟，退出时自动安装 autoInstallOnAppQuit）
 * - 检查/下载失败：静默记日志，不弹窗打扰（手动入口始终可用）
 * - 开发模式（未打包）不启用，避免签名/发布源缺失的噪音错误
 */
import { app } from 'electron';
import { autoUpdater } from 'electron-updater';
import type { GatewayLogger } from './types';

export type UpdatePhase =
  | 'checking'
  | 'available'
  | 'not-available'
  | 'downloading'
  | 'downloaded'
  | 'error';

export interface UpdateEvent {
  phase: UpdatePhase;
  version?: string;
  percent?: number;
  error?: string;
}

export interface UpdaterDeps {
  logger: GatewayLogger;
  broadcast(event: UpdateEvent): void;
  onDownloaded(): void; // 托盘通知
}

const STARTUP_CHECK_DELAY_MS = 15_000;

export class UpdaterService {
  private wired = false;
  private startupTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(private readonly deps: UpdaterDeps) {}

  get enabled(): boolean {
    return app.isPackaged;
  }

  /** 装配事件监听 + 启动后延迟静默检查 */
  init(): void {
    if (!this.enabled) {
      this.deps.logger.info('开发模式：自动更新未启用');
      return;
    }
    this.wire();
    this.startupTimer = setTimeout(() => {
      this.startupTimer = null;
      void this.check(true);
    }, STARTUP_CHECK_DELAY_MS);
  }

  /** silent=true 时错误只记日志；手动检查回传可读结果 */
  async check(silent: boolean): Promise<UpdateEvent> {
    if (!this.enabled) {
      return { phase: 'error', error: 'auto-update is disabled in development builds' };
    }
    this.wire();
    try {
      this.deps.broadcast({ phase: 'checking' });
      const result = await autoUpdater.checkForUpdates();
      const info = result?.updateInfo;
      if (!info) {
        return { phase: 'not-available' };
      }
      return { phase: 'available', version: info.version };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      if (!silent) {
        this.deps.logger.warn('手动检查更新失败:', message);
      } else {
        this.deps.logger.info('静默检查更新失败（网络/发布源不可达）:', message);
      }
      this.deps.broadcast({ phase: 'error', error: message });
      return { phase: 'error', error: message };
    }
  }

  quitAndInstall(): void {
    if (!this.enabled) return;
    // 先标记退出流程（主窗口 close=隐藏 让位于真正退出）
    app.quit();
    autoUpdater.quitAndInstall(false, true);
  }

  dispose(): void {
    if (this.startupTimer !== null) {
      clearTimeout(this.startupTimer);
      this.startupTimer = null;
    }
  }

  private wire(): void {
    if (this.wired) return;
    this.wired = true;

    autoUpdater.autoDownload = true;
    autoUpdater.autoInstallOnAppQuit = true;

    autoUpdater.on('update-available', (info) => {
      this.deps.broadcast({ phase: 'available', version: info.version });
    });
    autoUpdater.on('update-not-available', () => {
      this.deps.broadcast({ phase: 'not-available' });
    });
    autoUpdater.on('download-progress', (p) => {
      this.deps.broadcast({ phase: 'downloading', percent: Math.round(p.percent) });
    });
    autoUpdater.on('update-downloaded', (info) => {
      this.deps.broadcast({ phase: 'downloaded', version: info.version });
      this.deps.onDownloaded();
    });
    autoUpdater.on('error', (err) => {
      const message = err instanceof Error ? err.message : String(err);
      this.deps.logger.info('自动更新错误（静默）:', message);
      this.deps.broadcast({ phase: 'error', error: message });
    });
  }
}
