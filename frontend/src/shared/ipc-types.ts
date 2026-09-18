/**
 * IPC / Intent 共享类型（主进程 ↔ preload ↔ 渲染进程）
 *
 * 后端 WS 协议消息类型见 main/types.ts（此处转发导出）；
 * 本文件补充跨进程桥的形状：Intent、ToastMessage、WsResponse。
 */
export * from '../main/types';

/**
 * 音频源偏好（结构化，settings-management spec）：
 * - device：回环设备源，`id=''` = 整个系统（默认设备）
 * - process：按应用进程捕获，`lastPid` 记录最近绑定的 PID（仅展示/重选参考，绑定以列表 PID 为准）
 */
export type AudioSourcePref =
  | { kind: 'device'; id: string }
  | { kind: 'process'; name: string; lastPid: number | null };

/** 默认音频源 = 整个系统（默认回环设备） */
export const DEFAULT_AUDIO_SOURCE: AudioSourcePref = { kind: 'device', id: '' };

/**
 * 音频源目标（控制协议 `set_audio_source.source` 的线上形状，与后端同词汇）：
 * 进程源携带精确 PID（同名多实例以 PID 绑定）；store 持久化使用 `lastPid`。
 */
export type AudioSourceTarget =
  | { kind: 'device'; id: string }
  | { kind: 'process'; pid: number; name: string };

/** UI/托盘/快捷键统一动作入口（client-gateway-state spec：三源同 dispatch） */
export type Intent =
  | { type: 'togglePause' }
  | { type: 'cycleLanguage' }
  | { type: 'cycleModel' }
  | { type: 'setLanguage'; language: string }
  | { type: 'setModel'; model: string }
  | { type: 'setLlm'; modelId: string }
  | { type: 'toggleLock' }
  | { type: 'toggleOverlay' }
  | { type: 'setAudioSource'; source: AudioSourceTarget }
  | { type: 'setDevice'; device: 'auto' | 'cpu' | 'cuda' }
  | { type: 'newSession' }
  | { type: 'showSettings' }
  | { type: 'restartBackend' };

/** 渲染进程可写配置路径白名单（app:setConfig） */
export const WRITABLE_CONFIG_PATHS: readonly string[] = [
  'theme',
  'locked',
  'subtitle',
  'subtitle.fontFamily',
  'subtitle.fontSize',
  'subtitle.fontColor',
  'subtitle.strokeColor',
  'subtitle.strokeWidth',
  'subtitle.displayMode',
  'subtitle.preset',
  'translation',
  'translation.targetLanguages',
  'translation.activeLanguage',
  'translation.model',
  'asr.model',
  'inference.device',
  'audio.source',
  'window.opacity',
  'window.displayId',
  'shortcuts',
  'system.autoStart',
  'sessions.autoSplitSilenceMin',
  'onboarding.completed',
  'ui.sidebarCollapsed'
];

/** 显示器信息（app:getDisplays，多显示器选择用） */
export interface DisplayInfo {
  id: number;
  label: string;
  primary: boolean;
  workArea: { x: number; y: number; width: number; height: number };
}

/** 自动更新事件（app:update 广播） */
export interface UpdateEventView {
  phase: 'checking' | 'available' | 'not-available' | 'downloading' | 'downloaded' | 'error';
  version?: string;
  percent?: number;
  error?: string;
}

/** 主进程环境信息（渲染进程按能力降级 UI） */
export interface EnvInfo {
  platform: NodeJS.Platform;
  supportsWco: boolean;
  supportsAcrylic: boolean;
  version: string;
}

/** 瞬态提示（过载告警/后端错误/切换回执）——持久事实走 AppState，瞬态走 toast */
export interface ToastMessage {
  text: string;
  kind: 'info' | 'warn' | 'error';
}

/** wsRequest 桥的可序列化响应包 */
export type WsResponse<T = unknown> =
  | { ok: true; result: T }
  | { ok: false; error: string; message: string };
