/**
 * Preload — 渲染进程唯一 IPC 通道（contextIsolation 安全边界）
 *
 * `appAPI`：状态快照/patch 订阅/字幕流/toast/意图派发/WS 请求桥/配置写入/导航深链。
 * 渲染进程零 node 权限：MUST NOT 暴露 ipcRenderer 原始对象。
 */
import { contextBridge, ipcRenderer } from 'electron';
import type { AppState } from '../main/state';
import type { AppConfig } from '../main/config-migration';
import type {
  Intent, ToastMessage, WsResponse, EnvInfo, DisplayInfo
} from '../shared/ipc-types';
import type { SubtitleMessage } from '../shared/ipc-types';
import type { UpdateEvent } from '../main/updater';

type Unsubscribe = () => void;

function subscribe<T>(channel: string, cb: (payload: T) => void): Unsubscribe {
  const listener = (_event: Electron.IpcRendererEvent, payload: T): void => cb(payload);
  ipcRenderer.on(channel, listener);
  return () => {
    ipcRenderer.removeListener(channel, listener);
  };
}

contextBridge.exposeInMainWorld('appAPI', {
  // 状态
  getState: (): Promise<AppState> => ipcRenderer.invoke('app:getState'),
  onStatePatch: (cb: (patch: Partial<AppState>) => void): Unsubscribe =>
    subscribe('app:statePatch', cb),

  // 配置（字幕样式等偏好）
  getConfig: (): Promise<AppConfig> => ipcRenderer.invoke('app:getConfig'),
  onConfigChanged: (cb: (config: AppConfig) => void): Unsubscribe =>
    subscribe('app:configChanged', cb),

  // 流事件
  onSubtitle: (cb: (subtitle: SubtitleMessage) => void): Unsubscribe =>
    subscribe('app:subtitle', cb),
  onToast: (cb: (toast: ToastMessage) => void): Unsubscribe =>
    subscribe('app:toast', cb),

  // 动作（与托盘/快捷键同源的 dispatch 入口）
  dispatch: (intent: Intent): Promise<void> => ipcRenderer.invoke('app:intent', intent),

  // WS 请求桥（get_audio_sources / get_config ...）
  wsRequest: (method: string, params?: unknown): Promise<WsResponse> =>
    ipcRenderer.invoke('app:wsRequest', method, params),

  // 环境能力（WCO/亚克力支持探测，渲染层按能力降级）
  getEnv: (): Promise<EnvInfo> => ipcRenderer.invoke('app:getEnv'),

  // 配置写入（改动即落盘；主进程按白名单校验并广播 configChanged）
  setConfig: (path: string, value: unknown): Promise<boolean> =>
    ipcRenderer.invoke('app:setConfig', path, value),

  // 深链导航（托盘"设置" → 主窗口设置页等）
  onNavigate: (cb: (route: string) => void): Unsubscribe => subscribe('app:navigate', cb),

  // 系统目录入口（设置"高级"分段）
  openPath: (kind: 'configDir' | 'logDir'): Promise<boolean> =>
    ipcRenderer.invoke('app:openPath', kind),

  // 自动更新（release-packaging spec）
  checkUpdate: (): Promise<UpdateEvent> => ipcRenderer.invoke('app:checkUpdate'),
  onUpdate: (cb: (event: UpdateEvent) => void): Unsubscribe => subscribe('app:update', cb),
  quitAndInstall: (): Promise<void> => ipcRenderer.invoke('app:quitAndInstall'),

  // 多显示器（悬浮窗显示器选择）
  getDisplays: (): Promise<DisplayInfo[]> => ipcRenderer.invoke('app:getDisplays'),

  // 会话历史（session-history spec）
  listSessions: (): Promise<unknown> => ipcRenderer.invoke('history:listSessions'),
  getSession: (id: string): Promise<unknown> => ipcRenderer.invoke('history:getSession', id),
  searchHistory: (query: string): Promise<unknown> =>
    ipcRenderer.invoke('history:search', query),
  renameSession: (id: string, title: string): Promise<boolean> =>
    ipcRenderer.invoke('history:rename', id, title),
  deleteSession: (id: string): Promise<unknown> =>
    ipcRenderer.invoke('history:delete', id),
  newSession: (): Promise<string | null> => ipcRenderer.invoke('history:newSession'),
  historyStats: (): Promise<unknown> => ipcRenderer.invoke('history:stats'),
  clearHistory: (): Promise<unknown> => ipcRenderer.invoke('history:clear'),
  exportSession: (
    id: string, format: 'srt' | 'txt' | 'md' | 'json'
  ): Promise<unknown> => ipcRenderer.invoke('history:export', id, format),
  onHistoryChanged: (cb: (info: { kind: string; sessionId?: string }) => void): Unsubscribe =>
    subscribe('history:changed', cb)
});
