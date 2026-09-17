/**
 * 渲染进程桥类型（window.appAPI / window.electronAPI 的本地结构声明）
 * 与主进程 AppState/AppConfig 结构对齐；contextBridge 序列化边界，渲染层不 import 主进程模块。
 */

interface AppStateView {
  connection: 'connecting' | 'open' | 'reconnecting' | 'down';
  capture: 'running' | 'paused';
  vad: 'speech' | 'silence' | 'unknown';
  model: string;
  modelDownload: { name: string; progress: number; message: string } | null;
  activeLanguage: string;
  targetLanguages: string[];
  audioSource: string;
  locked: boolean;
  overlayVisible: boolean;
  lastWarning: { droppedTotal: number; at: number } | null;
  droppedCount: number;
  activeSessionId: string | null;
}

interface SubtitleStyleView {
  fontFamily: string;
  fontSize: number;
  fontColor: string;
  strokeColor: string;
  strokeWidth: number;
  displayMode: 'translation_only' | 'original_and_translation';
  preset: 'text' | 'acrylic';
}

interface AppConfigView {
  window: {
    width: number;
    height: number;
    x: number | null;
    y: number | null;
    opacity: number;
    displayId: number | null;
    positions: Record<string, { x: number; y: number }>;
  };
  subtitle: SubtitleStyleView;
  websocket: { host: string; port: number };
  shortcuts: {
    togglePause: string;
    switchLanguage: string;
    switchModel: string;
    toggleLock: string;
  };
  /** 全局快捷键注册状态（false=被占用，设置页警示） */
  shortcutStatus: {
    togglePause: boolean;
    switchLanguage: boolean;
    switchModel: boolean;
    toggleLock: boolean;
  };
  translation: { targetLanguages: string[]; activeLanguage: string };
  asr: { model: string };
  audio: { sourceId: string };
  locked: boolean;
  theme: 'dark' | 'light' | 'system';
  system: { autoStart: boolean };
  sessions: { autoSplitSilenceMin: number | null };
  ui: {
    sidebarCollapsed: boolean;
    mainWindow: { width: number; height: number; x: number | null; y: number | null };
  };
  onboarding: { completed: boolean };
}

interface SubtitleMessageView {
  type: 'subtitle';
  original: string;
  source_language: string;
  active_language: string;
  translations: Record<string, string>;
  ts_start?: number;
  ts_end?: number;
}

interface ToastView {
  text: string;
  kind: 'info' | 'warn' | 'error';
}

type WsResponseView =
  | { ok: true; result: unknown }
  | { ok: false; error: string; message: string };

type Unsubscribe = () => void;

interface SessionRowView {
  id: string;
  title: string;
  started_at: number;
  ended_at: number | null;
  audio_source: string;
  utterance_count: number;
}

interface UtteranceRowView {
  id: number;
  session_id: string;
  received_at: number;
  ts_start: number | null;
  ts_end: number | null;
  original: string;
  source_lang: string;
  translation: string;
  target_lang: string;
  model: string;
}

interface SessionDetailView {
  session: SessionRowView;
  utterances: UtteranceRowView[];
}

interface SearchHitView {
  utterance: UtteranceRowView;
  sessionTitle: string;
  sessionStartedAt: number;
  snippet: string;
  matchedField: 'original' | 'translation';
}

interface HistoryStatsView {
  sizeBytes: number;
  sessionCount: number;
  utteranceCount: number;
}

interface ExportResultView {
  ok: boolean;
  filePath?: string;
  error?: string;
  approximate?: boolean;
}

interface EnvView {
  platform: string;
  supportsWco: boolean;
  supportsAcrylic: boolean;
  version: string;
}

interface DisplayView {
  id: number;
  label: string;
  primary: boolean;
  workArea: { x: number; y: number; width: number; height: number };
}

interface UpdateEventView {
  phase: 'checking' | 'available' | 'not-available' | 'downloading' | 'downloaded' | 'error';
  version?: string;
  percent?: number;
  error?: string;
}

interface AppAPI {
  getState(): Promise<AppStateView>;
  onStatePatch(cb: (patch: Partial<AppStateView>) => void): Unsubscribe;
  getConfig(): Promise<AppConfigView>;
  onConfigChanged(cb: (config: AppConfigView) => void): Unsubscribe;
  onSubtitle(cb: (subtitle: SubtitleMessageView) => void): Unsubscribe;
  onToast(cb: (toast: ToastView) => void): Unsubscribe;
  dispatch(intent: { type: string; [key: string]: unknown }): Promise<void>;
  wsRequest(method: string, params?: unknown): Promise<WsResponseView>;
  getEnv(): Promise<EnvView>;
  setConfig(path: string, value: unknown): Promise<boolean>;
  onNavigate(cb: (route: string) => void): Unsubscribe;
  openPath(kind: 'configDir' | 'logDir'): Promise<boolean>;
  checkUpdate(): Promise<UpdateEventView>;
  onUpdate(cb: (event: UpdateEventView) => void): Unsubscribe;
  quitAndInstall(): Promise<void>;
  getDisplays(): Promise<DisplayView[]>;
  // 会话历史
  listSessions(): Promise<SessionRowView[]>;
  getSession(id: string): Promise<SessionDetailView | null>;
  searchHistory(query: string): Promise<SearchHitView[]>;
  renameSession(id: string, title: string): Promise<boolean>;
  deleteSession(id: string): Promise<{ deleted: boolean; wasActive: boolean }>;
  newSession(): Promise<string | null>;
  historyStats(): Promise<HistoryStatsView>;
  clearHistory(): Promise<HistoryStatsView>;
  exportSession(
    id: string, format: 'srt' | 'txt' | 'md' | 'json'
  ): Promise<ExportResultView>;
  onHistoryChanged(cb: (info: { kind: string; sessionId?: string }) => void): Unsubscribe;
}

interface Window {
  appAPI: AppAPI;
}
