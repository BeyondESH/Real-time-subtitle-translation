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

export interface PipelineWarningMessage {
  type: 'pipeline_warning';
  reason?: string;
  /** 后端累计丢弃数 */
  dropped?: number;
  message?: string;
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

export type KnownBroadcast =
  | SubtitleMessage
  | ModelProgressMessage
  | PipelineWarningMessage
  | BackendErrorMessage
  | VadStateMessage;

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
