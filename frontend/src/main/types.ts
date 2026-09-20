/**
 * 主进程共享类型：后端 WS 协议消息 + 连接状态
 *
 * 协议对应 backend/websocket_server.py：
 * - 广播：subtitle / subtitle_partial / subtitle_cancel / model_progress /
 *   pipeline_warning / error / vad_state
 * - 控制：{type:'control', action, ...}；同步：{type:'config_sync', ...}
 * - 请求/响应：{type:'request', id, method, params} / {type:'response', id, ok, result|error}
 */

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'down';

/** 分阶段耗时（毫秒，非负整数）；对象全有或全无（5 键齐全，reduce-pipeline-latency） */
export interface SubtitleLatency {
  /** 切句时尾部静音时长（未加 pad 起算） */
  endpoint_ms: number;
  /** 入队到开始处理 */
  queue_ms: number;
  /** 识别耗时 */
  asr_ms: number;
  /** 翻译耗时（含退化重试） */
  llm_ms: number;
  /** 切句到广播 */
  total_ms: number;
}

export interface SubtitleMessage {
  type: 'subtitle';
  original: string;
  source_language: string;
  active_language: string;
  translations: Record<string, string>;
  /** 后端 P2 起透传切句时间轴（秒）；旧后端缺失 */
  ts_start?: number;
  ts_end?: number;
  /** LLM 生成速度（tok/s，add-llm-tps-to-subtitles）；翻译失败/旧后端缺失 */
  tps?: number;
  /** 分阶段耗时（reduce-pipeline-latency）；兼容路径/旧后端缺失 */
  latency?: SubtitleLatency;
  /** 语句标识（add-llm-streaming-output）；旧后端缺失 */
  id?: string;
  /** 切句→首个进行中帧时延（毫秒，非负整数）；非流式/旧后端缺失 */
  first_token_ms?: number;
}

/** 进行中帧（add-llm-streaming-output；MUST NOT 落库）：同一句多次广播共享同一 id */
export interface SubtitlePartialMessage {
  type: 'subtitle_partial';
  id: string;
  original: string;
  source_language: string;
  active_language: string;
  translations: Record<string, string>;
}

/** 清算帧（add-llm-streaming-output）：进行中帧的终结；reason 开放，消费方容错未知值 */
export interface SubtitleCancelMessage {
  type: 'subtitle_cancel';
  id: string;
  reason?: string;
}

/** 字幕流事件（进行中 / 清算 / 定稿）：preload 与渲染层透传的联合 */
export type SubtitleStreamEvent =
  | SubtitleMessage
  | SubtitlePartialMessage
  | SubtitleCancelMessage;

export interface ModelProgressMessage {
  type: 'model_progress';
  model_name: string;
  progress: number;
  message: string;
}

/**
 * 告警原因（design D5）：既有 queue_full 语义保留，新增 stalled / engine_degraded。
 * 开放联合：`(string & {})` 保留字面量补全，同时允许后端未来新增 reason 透传。
 */
export type PipelineWarningReason =
  | 'queue_full'
  | 'stalled'
  | 'engine_degraded'
  | (string & {});

export interface PipelineWarningMessage {
  type: 'pipeline_warning';
  reason?: PipelineWarningReason;
  /** 后端累计丢弃数（旧后端 / 未知 reason 可能缺失，消费方需容错） */
  dropped?: number;
  message?: string;
  /** 停滞时队列深度（D5 纯增量字段，可选） */
  pending?: number;
  /** 结构化补充信息，如 engine_degraded 的 {engine, from, to, device_reason} */
  detail?: Record<string, unknown>;
}

export interface BackendErrorMessage {
  type: 'error';
  code?: string;
  message?: string;
}

/** 后端 P2 起广播；旧后端不发送，消费方需容错缺失 */
export interface VadStateMessage {
  type: 'vad_state';
  state: 'speech' | 'silence';
}

/** 实际解析出的设备（null = 加载/检测中） */
export type DeviceResolved = 'cuda' | 'cpu' | null;

/** 设备原因：用户指定 / 自动检测成功 / 无 CUDA / GPU 加载失败 / 运行期失败降级 */
export type DeviceReason = 'auto' | 'user' | 'no_cuda' | 'load_failed' | 'runtime_failed';

export interface DeviceEngineState {
  resolved: DeviceResolved;
  reason: DeviceReason;
}

/**
 * 后端 device_state 广播（add-inference-device-toggle D8）。
 * 旧后端不发送（type 不存在），新后端字段完整；消费方对缺失字段容错。
 */
export interface DeviceStateMessage {
  type: 'device_state';
  asr: DeviceEngineState;
  translation: DeviceEngineState;
}

/** device_state 去掉 type 后的视图（AppState.device / get_config 种子形状） */
export type DeviceStateView = Omit<DeviceStateMessage, 'type'>;

export type KnownBroadcast =
  | SubtitleMessage
  | SubtitlePartialMessage
  | SubtitleCancelMessage
  | ModelProgressMessage
  | PipelineWarningMessage
  | BackendErrorMessage
  | VadStateMessage
  | DeviceStateMessage;

export type UnknownBroadcast = { type: string } & Record<string, unknown>;

export interface ResponseMessage {
  type: 'response';
  id: string;
  ok: boolean;
  result?: unknown;
  error?: string;
}

export interface GatewayLogger {
  info(...args: unknown[]): void;
  warn(...args: unknown[]): void;
  error(...args: unknown[]): void;
}
