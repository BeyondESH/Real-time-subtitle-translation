/**
 * 渲染进程桥类型（window.appAPI / window.electronAPI 的本地结构声明）
 * 与主进程 AppState/AppConfig 结构对齐；contextBridge 序列化边界，渲染层不 import 主进程模块。
 */

interface DeviceEngineStateView {
  resolved: 'cuda' | 'cpu' | null;
  reason: 'auto' | 'user' | 'no_cuda' | 'load_failed' | 'runtime_failed';
}

interface DeviceStateView {
  asr: DeviceEngineStateView;
  translation: DeviceEngineStateView;
}

/** 最近一次 pipeline_warning 视图（design D5；reason 开放，未知值需泛化呈现） */
interface WarningInfoView {
  droppedTotal: number;
  at: number;
  reason?: string;
  message?: string;
  pending?: number;
  detail?: Record<string, unknown>;
}

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
  lastWarning: WarningInfoView | null;
  droppedCount: number;
  /** 后端上报的实际推理设备（null = 尚未上报/检测中） */
  device: DeviceStateView | null;
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

/** 音频源偏好（渲染边界本地声明，与主进程 AudioSourcePref 结构对齐） */
type AudioSourcePrefView = { kind: 'device'; id: string };

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
    toggleLock: string;
  };
  /** 全局快捷键注册状态（false=被占用，设置页警示） */
  shortcutStatus: {
    togglePause: boolean;
    switchLanguage: boolean;
    toggleLock: boolean;
  };
  translation: { targetLanguages: string[]; activeLanguage: string; model: string };
  /** 源语言偏好（识别提示 + 翻译源语言；识别引擎为 Fun-ASR-Nano 单模型） */
  asr: { language: 'ja' | 'zh' | 'en' };
  /** 推理设备偏好（auto/cpu/cuda） */
  inference: { device: 'auto' | 'cpu' | 'cuda' };
  /** 音频源偏好（结构化：设备源或按进程源） */
  audio: { source: AudioSourcePrefView };
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
  /** LLM 生成速度（tok/s）；缺失（翻译失败/旧后端）时脚注不显示 */
  tps?: number;
  /** 分阶段耗时（毫秒，非负整数；全有或全无）；缺失（兼容路径/旧后端）时脚注不显示 */
  latency?: {
    endpoint_ms: number;
    queue_ms: number;
    asr_ms: number;
    llm_ms: number;
    total_ms: number;
  };
  /** 语句标识（add-llm-streaming-output）；旧后端缺失 */
  id?: string;
  /** 切句→首个进行中帧时延（毫秒，非负整数）；非流式/旧后端缺失 */
  first_token_ms?: number;
}

/** 进行中帧（add-llm-streaming-output；MUST NOT 落库） */
interface SubtitlePartialMessageView {
  type: 'subtitle_partial';
  id: string;
  original: string;
  source_language: string;
  active_language: string;
  translations: Record<string, string>;
}

/** 清算帧（add-llm-streaming-output）：reason 开放，未知值容错 */
interface SubtitleCancelMessageView {
  type: 'subtitle_cancel';
  id: string;
  reason?: string;
}

/** 字幕流事件（进行中 / 清算 / 定稿）：app:subtitle 通道载荷联合 */
type SubtitleStreamEventView =
  | SubtitleMessageView
  | SubtitlePartialMessageView
  | SubtitleCancelMessageView;

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
  onSubtitle(cb: (subtitle: SubtitleStreamEventView) => void): Unsubscribe;
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
