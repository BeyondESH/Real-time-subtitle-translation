/**
 * 主进程共享类型：后端 WS 协议消息 + 连接状态
 *
 * 协议对应 backend/websocket_server.py：
 * - 广播：subtitle / model_progress / pipeline_warning / error / vad_state
 * - 控制：{type:'control', action, ...}；同步：{type:'config_sync', ...}
 * - 请求/响应：{type:'request', id, method, params} / {type:'response', id, ok, result|error}
 */

export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'down';

export interface SubtitleMessage {
  type: 'subtitle';
  original: string;
  source_language: string;
  active_language: string;
  translations: Record<string, string>;
  /** 后端 P2 起透传切句时间轴（秒）；旧后端缺失 */
  ts_start?: number;
  ts_end?: number;
}

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

/**
 * 音频源退出广播（pipeline-control spec：按进程捕获目标退出并回退后触发）。
 * 事件驱动；无客户端连接时后端跳过广播，消费方对未知类型安全。
 */
export interface AudioSourceLostMessage {
  type: 'audio_source_lost';
  name: string;
  pid: number;
  fallback: 'system';
}

export type KnownBroadcast =
  | SubtitleMessage
  | ModelProgressMessage
  | PipelineWarningMessage
  | BackendErrorMessage
  | VadStateMessage
  | DeviceStateMessage
  | AudioSourceLostMessage;

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
