/**
 * IPC / Intent 共享类型（主进程 ↔ preload ↔ 渲染进程）
 *
 * 后端 WS 协议消息类型见 main/types.ts（此处转发导出）；
 * 本文件补充跨进程桥的形状：Intent、ToastMessage、WsResponse。
 */
export * from '../main/types';

/**
 * 音频源偏好（结构化设备源，settings-management spec）：
 * `id=''` = 默认回环设备（整个系统）。
 */
export type AudioSourcePref = { kind: 'device'; id: string };

/** 默认音频源 = 默认回环设备（整个系统） */
export const DEFAULT_AUDIO_SOURCE: AudioSourcePref = { kind: 'device', id: '' };

/** 音频源目标（控制协议 `set_audio_source.source` 的线上形状，与后端同词汇） */
export type AudioSourceTarget = { kind: 'device'; id: string };

/** 源语言（识别提示 + 翻译源语言；Fun-ASR-Nano 官方支持范围，spec: language-handling） */
export type SourceLanguage = 'ja' | 'zh' | 'en';

export const SOURCE_LANGUAGES: readonly SourceLanguage[] = ['ja', 'zh', 'en'];
export const DEFAULT_SOURCE_LANGUAGE: SourceLanguage = 'ja';

export const SOURCE_LANGUAGE_LABELS: Record<SourceLanguage, string> = {
  ja: '日语',
  zh: '中文',
  en: '英文'
};

/** 识别引擎显示名（单引擎模型，spec: main-window「状态胶囊条」） */
export const ENGINE_MODEL_DISPLAY = 'Fun-ASR-Nano';

/** UI/托盘/快捷键统一动作入口（client-gateway-state spec：三源同 dispatch） */
export type Intent =
  | { type: 'togglePause' }
  | { type: 'cycleLanguage' }
  | { type: 'setLanguage'; language: string }
  | { type: 'setSourceLanguage'; language: SourceLanguage }
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
  'asr',
  'asr.language',
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
