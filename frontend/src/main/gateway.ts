/**
 * Gateway — 主进程到后端的唯一 WebSocket 客户端
 *
 * 职责（client-gateway-state spec）：
 * - 连接生命周期：指数退避重连（250ms → 30s 封顶），状态 connecting/open/reconnecting/down
 * - request(method,params) → Promise：唯一 id + pending map + 超时（默认 10s）
 * - 广播解析与类型化分发（subtitle/model_progress/pipeline_warning/error/vad_state）
 *
 * 纯 Node 模块：不 import electron（日志经注入 logger），可独立单测。
 */
import { randomUUID } from 'crypto';
import WebSocket from 'ws';
import type {
  ConnectionState, GatewayLogger, KnownBroadcast, ResponseMessage, UnknownBroadcast
} from './types';

/** 可注入的 socket 抽象：生产用 ws，测试用 fake */
export interface WsLike {
  readonly readyState: number;
  send(data: string): void;
  close(): void;
  attach(handlers: {
    open(): void;
    message(raw: unknown): void;
    close(): void;
    error(err: unknown): void;
  }): void;
}

export type Transport = (url: string) => WsLike;

export type GatewayEvent =
  | { kind: 'state'; state: ConnectionState }
  | { kind: 'broadcast'; message: KnownBroadcast | UnknownBroadcast };

export class GatewayError extends Error {
  readonly code: string;
  constructor(message: string, code = 'gateway_error') {
    super(message);
    this.name = 'GatewayError';
    this.code = code;
  }
}

export interface GatewayOptions {
  url: string;
  transport?: Transport;
  logger?: GatewayLogger;
  initialDelayMs?: number;
  maxDelayMs?: number;
  backoffFactor?: number;
  requestTimeoutMs?: number;
  idFactory?: () => string;
}

const WS_OPEN = 1;

/** 重连退避：min(initial * factor^attempt, max) */
export function retryDelayMs(
  attempt: number, initial = 250, factor = 1.5, max = 30000
): number {
  return Math.min(Math.floor(initial * Math.pow(factor, attempt)), max);
}

function toText(raw: unknown): string {
  if (typeof raw === 'string') return raw;
  if (Buffer.isBuffer(raw)) return raw.toString('utf8');
  if (raw instanceof ArrayBuffer) return Buffer.from(raw).toString('utf8');
  if (Array.isArray(raw)) return Buffer.concat(raw as Buffer[]).toString('utf8');
  return String(raw);
}

interface PendingRequest {
  resolve(value: unknown): void;
  reject(err: Error): void;
  timer: ReturnType<typeof setTimeout>;
  method: string;
}

const defaultLogger: GatewayLogger = {
  info: (...a: unknown[]) => console.log('[gateway]', ...a),
  warn: (...a: unknown[]) => console.warn('[gateway]', ...a),
  error: (...a: unknown[]) => console.error('[gateway]', ...a)
};

function defaultTransport(url: string): WsLike {
  const ws = new WebSocket(url);
  return {
    get readyState() {
      return ws.readyState;
    },
    send: (data: string) => { ws.send(data); },
    close: () => { ws.close(); },
    attach: (handlers) => {
      ws.on('open', handlers.open);
      ws.on('message', (data) => handlers.message(data));
      ws.on('close', handlers.close);
      ws.on('error', (err) => handlers.error(err));
    }
  };
}

export class Gateway {
  private readonly url: string;
  private readonly transport: Transport;
  private readonly logger: GatewayLogger;
  private readonly initialDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly backoffFactor: number;
  private readonly requestTimeoutMs: number;
  private readonly idFactory: () => string;

  private socket: WsLike | null = null;
  private generation = 0;
  private attempt = 0;
  private hasOpened = false;
  private stopped = true;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private _state: ConnectionState = 'down';
  private readonly pending = new Map<string, PendingRequest>();
  private readonly listeners = new Set<(e: GatewayEvent) => void>();

  constructor(opts: GatewayOptions) {
    this.url = opts.url;
    this.transport = opts.transport ?? defaultTransport;
    this.logger = opts.logger ?? defaultLogger;
    this.initialDelayMs = opts.initialDelayMs ?? 250;
    this.maxDelayMs = opts.maxDelayMs ?? 30000;
    this.backoffFactor = opts.backoffFactor ?? 1.5;
    this.requestTimeoutMs = opts.requestTimeoutMs ?? 10000;
    this.idFactory = opts.idFactory ?? randomUUID;
  }

  get state(): ConnectionState {
    return this._state;
  }

  /** 启动连接生命周期（幂等：已在运行则忽略） */
  connect(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempt = 0;
    this.hasOpened = false;
    this.setState('connecting');
    this.openSocket();
  }

  /** 主动停止（应用退出/后端托管重启前调用），不再重连 */
  close(): void {
    this.stopped = true;
    this.clearRetry();
    this.generation += 1; // 使现有 socket 的回调全部失效
    const sock = this.socket;
    this.socket = null;
    this.rejectAllPending(new GatewayError('gateway closed', 'closed'));
    if (sock) {
      try { sock.close(); } catch (err) { this.logger.warn('socket close failed', err); }
    }
    this.setState('down');
  }

  /** 发送控制/同步消息（fire-and-forget）；未连接返回 false */
  send(payload: Record<string, unknown>): boolean {
    if (!this.socket || this.socket.readyState !== WS_OPEN) return false;
    try {
      this.socket.send(JSON.stringify(payload));
      return true;
    } catch (err) {
      this.logger.error('send failed', err);
      return false;
    }
  }

  /** 请求/响应桥：超时与 ok:false 均以类型化 GatewayError 拒绝 */
  request<T = unknown>(method: string, params?: unknown): Promise<T> {
    if (!this.socket || this.socket.readyState !== WS_OPEN) {
      return Promise.reject(new GatewayError('backend not connected', 'not_connected'));
    }
    const id = this.idFactory();
    const payload: Record<string, unknown> = { type: 'request', id, method };
    if (params !== undefined) payload.params = params;

    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new GatewayError(`request timeout: ${method}`, 'timeout'));
      }, this.requestTimeoutMs);
      this.pending.set(id, {
        resolve: resolve as (v: unknown) => void, reject, timer, method
      });
      if (!this.send(payload)) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new GatewayError(`send failed: ${method}`, 'not_connected'));
      }
    });
  }

  /** 订阅连接状态与广播事件，返回退订函数 */
  onEvent(cb: (e: GatewayEvent) => void): () => void {
    this.listeners.add(cb);
    return () => { this.listeners.delete(cb); };
  }

  // ---------- 内部 ----------

  private openSocket(): void {
    const gen = ++this.generation;
    let sock: WsLike;
    try {
      sock = this.transport(this.url);
    } catch (err) {
      this.logger.error('transport create failed', err);
      this.scheduleRetry();
      return;
    }
    this.socket = sock;
    sock.attach({
      open: () => {
        if (gen !== this.generation) return;
        this.hasOpened = true;
        this.attempt = 0;
        this.clearRetry();
        this.setState('open');
      },
      message: (raw) => {
        if (gen !== this.generation) return;
        this.handleMessage(raw);
      },
      close: () => {
        if (gen !== this.generation) return;
        this.handleSocketClose();
      },
      error: (err) => {
        if (gen !== this.generation) return;
        this.logger.warn('ws error', err);
      }
    });
  }

  private handleSocketClose(): void {
    this.socket = null;
    this.rejectAllPending(new GatewayError('connection closed', 'closed'));
    if (this.stopped) {
      this.setState('down');
      return;
    }
    this.scheduleRetry();
  }

  private scheduleRetry(): void {
    const delay = retryDelayMs(
      this.attempt, this.initialDelayMs, this.backoffFactor, this.maxDelayMs
    );
    this.attempt += 1;
    this.setState(this.hasOpened ? 'reconnecting' : 'connecting');
    this.clearRetry();
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (!this.stopped) this.openSocket();
    }, delay);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  private handleMessage(raw: unknown): void {
    let data: unknown;
    try {
      data = JSON.parse(toText(raw));
    } catch (err) {
      this.logger.warn('malformed message ignored', err);
      return;
    }
    if (typeof data !== 'object' || data === null || typeof (data as { type?: unknown }).type !== 'string') {
      this.logger.warn('non-message payload ignored', data);
      return;
    }
    const msg = data as Record<string, unknown>;

    if (msg.type === 'response') {
      const resp = msg as unknown as ResponseMessage;
      const pendingItem = this.pending.get(resp.id);
      if (!pendingItem) return; // 超时后迟到的响应直接丢弃
      this.pending.delete(resp.id);
      clearTimeout(pendingItem.timer);
      if (resp.ok) {
        pendingItem.resolve(resp.result);
      } else {
        pendingItem.reject(new GatewayError(resp.error ?? 'backend error', 'backend_error'));
      }
      return;
    }

    this.emit({ kind: 'broadcast', message: msg as KnownBroadcast | UnknownBroadcast });
  }

  private rejectAllPending(err: Error): void {
    for (const [id, item] of this.pending) {
      clearTimeout(item.timer);
      item.reject(err);
      this.pending.delete(id);
    }
  }

  private setState(next: ConnectionState): void {
    if (this._state === next) return;
    this._state = next;
    this.emit({ kind: 'state', state: next });
  }

  private emit(e: GatewayEvent): void {
    for (const cb of [...this.listeners]) {
      try {
        cb(e);
      } catch (err) {
        this.logger.error('event listener threw', err);
      }
    }
  }
}
