/**
 * Gateway 单测：连接生命周期 / 退避重连 / 请求响应桥 / 广播路由 / 容错
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Gateway, GatewayError, retryDelayMs, type WsLike } from '../gateway';
import type { GatewayEvent } from '../gateway';

type Handlers = Parameters<WsLike['attach']>[0];

class FakeSocket implements WsLike {
  static readonly all: FakeSocket[] = [];
  readyState = 0; // CONNECTING
  readonly sent: string[] = [];
  closed = false;
  private handlers: Handlers | null = null;

  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }

  attach(h: Handlers): void {
    this.handlers = h;
  }

  send(d: string): void {
    this.sent.push(d);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.readyState = 3;
    this.handlers?.close();
  }

  // ---- 测试驱动 ----
  openNow(): void {
    this.readyState = 1;
    this.handlers?.open();
  }

  emit(msg: unknown): void {
    this.handlers?.message(JSON.stringify(msg));
  }

  emitBuffer(msg: unknown): void {
    this.handlers?.message(Buffer.from(JSON.stringify(msg), 'utf8'));
  }

  emitRaw(s: string): void {
    this.handlers?.message(s);
  }

  drop(): void {
    this.readyState = 3;
    this.handlers?.close();
  }

  sentJson(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }
}

const transport = (url: string): WsLike => new FakeSocket(url);

function makeGateway(overrides: Partial<ConstructorParameters<typeof Gateway>[0]> = {}) {
  let seq = 0;
  const warnings: unknown[][] = [];
  const gw = new Gateway({
    url: 'ws://localhost:8765',
    transport,
    logger: {
      info: () => undefined,
      warn: (...a: unknown[]) => { warnings.push(a); },
      error: () => undefined
    },
    idFactory: () => `id-${++seq}`,
    ...overrides
  });
  const events: GatewayEvent[] = [];
  gw.onEvent((e) => events.push(e));
  return { gw, warnings, events, socket: () => FakeSocket.all[FakeSocket.all.length - 1] };
}

beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.all.length = 0;
});

afterEach(() => {
  vi.useRealTimers();
});

describe('retryDelayMs', () => {
  it('指数退避且封顶 30s', () => {
    expect(retryDelayMs(0)).toBe(250);
    expect(retryDelayMs(1)).toBe(375);
    expect(retryDelayMs(2)).toBe(562);
    expect(retryDelayMs(30)).toBe(30000);
    expect(retryDelayMs(100)).toBe(30000);
  });
});

describe('连接生命周期', () => {
  it('connect 后状态 connecting → open，事件按序广播', () => {
    const { gw, events, socket } = makeGateway();
    expect(gw.state).toBe('down');
    gw.connect();
    expect(gw.state).toBe('connecting');
    socket().openNow();
    expect(gw.state).toBe('open');
    expect(events.filter((e) => e.kind === 'state').map((e) => (e as { state: string }).state))
      .toEqual(['connecting', 'open']);
    gw.close();
  });

  it('connect 幂等：重复调用不重建 socket', () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    const first = socket();
    gw.connect();
    expect(FakeSocket.all.length).toBe(1);
    expect(socket()).toBe(first);
    gw.close();
  });

  it('曾 open 后断线 → reconnecting，未 open 过断线 → 保持 connecting', () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    socket().drop();
    expect(gw.state).toBe('reconnecting');
    vi.advanceTimersByTime(250);
    const second = socket();
    second.drop(); // 从未 open
    expect(gw.state).toBe('reconnecting'); // hasOpened 会话内保持
    gw.close();
    expect(gw.state).toBe('down');
  });

  it('退避重连：250ms → 375ms，精确到边界', () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().drop();
    expect(FakeSocket.all.length).toBe(1);
    vi.advanceTimersByTime(249);
    expect(FakeSocket.all.length).toBe(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all.length).toBe(2);
    socket().drop();
    vi.advanceTimersByTime(374);
    expect(FakeSocket.all.length).toBe(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all.length).toBe(3);
    gw.close();
  });

  it('open 后重连成功：attempt 归零，下次断线从 250ms 起', () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    socket().drop();
    vi.advanceTimersByTime(250);
    expect(FakeSocket.all.length).toBe(2);
    socket().openNow();
    expect(gw.state).toBe('open');
    socket().drop();
    vi.advanceTimersByTime(250);
    expect(FakeSocket.all.length).toBe(3); // 从 initial 重新起步
    gw.close();
  });

  it('close 后不再重连', () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().drop();
    gw.close();
    vi.advanceTimersByTime(120000);
    expect(FakeSocket.all.length).toBe(1);
  });

  it('Buffer 消息正常解析', () => {
    const { gw, events, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    socket().emitBuffer({ type: 'vad_state', state: 'speech' });
    const broadcasts = events.filter((e) => e.kind === 'broadcast');
    expect(broadcasts.length).toBe(1);
    expect((broadcasts[0] as { message: { type: string } }).message.type).toBe('vad_state');
    gw.close();
  });

  it('畸形 JSON 不崩溃并记录告警', () => {
    const { gw, warnings, events, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    socket().emitRaw('{oops');
    socket().emitRaw('42');
    expect(warnings.length).toBeGreaterThanOrEqual(2);
    expect(events.filter((e) => e.kind === 'broadcast').length).toBe(0);
    gw.close();
  });
});

describe('请求/响应桥', () => {
  it('未连接时立即拒绝 not_connected', async () => {
    const { gw } = makeGateway();
    await expect(gw.request('get_config')).rejects.toMatchObject({ code: 'not_connected' });
  });

  it('成功路径：帧形状正确并按 id 路由 resolve', async () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    const p = gw.request<string[]>('get_audio_sources', { filter: 'loopback' });
    const frame = socket().sentJson()[0];
    expect(frame).toEqual({
      type: 'request', id: 'id-1', method: 'get_audio_sources', params: { filter: 'loopback' }
    });
    socket().emit({ type: 'response', id: 'id-1', ok: true, result: ['dev-a'] });
    await expect(p).resolves.toEqual(['dev-a']);
    gw.close();
  });

  it('ok:false 以 backend_error 拒绝并携带错误消息', async () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    const p = gw.request('get_config');
    socket().emit({ type: 'response', id: 'id-1', ok: false, error: '未知方法' });
    await expect(p).rejects.toBeInstanceOf(GatewayError);
    await expect(p).rejects.toMatchObject({ code: 'backend_error', message: '未知方法' });
    gw.close();
  });

  it('10s 超时拒绝，迟到响应被忽略', async () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    const p = gw.request('get_config');
    vi.advanceTimersByTime(10000);
    await expect(p).rejects.toMatchObject({ code: 'timeout' });
    expect(() => socket().emit({ type: 'response', id: 'id-1', ok: true, result: 1 })).not.toThrow();
    gw.close();
  });

  it('断连时全部 pending 被拒绝（closed）', async () => {
    const { gw, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    const p1 = gw.request('a');
    const p2 = gw.request('b');
    socket().drop();
    await expect(p1).rejects.toMatchObject({ code: 'closed' });
    await expect(p2).rejects.toMatchObject({ code: 'closed' });
    gw.close();
  });

  it('send 未连接返回 false，open 后帧进入 socket', () => {
    const { gw, socket } = makeGateway();
    expect(gw.send({ type: 'control', action: 'pause' })).toBe(false);
    gw.connect();
    socket().openNow();
    expect(gw.send({ type: 'control', action: 'pause' })).toBe(true);
    expect(socket().sentJson()[0]).toEqual({ type: 'control', action: 'pause' });
    gw.close();
  });
});

describe('广播路由与订阅', () => {
  it('subtitle / pipeline_warning / 未知类型全部按 broadcast 透出', () => {
    const { gw, events, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    socket().emit({ type: 'subtitle', original: 'a', source_language: 'ja', active_language: 'zh', translations: { zh: '甲' } });
    socket().emit({ type: 'pipeline_warning', reason: 'queue_full', dropped: 3, message: 'm' });
    socket().emit({ type: 'future_type', foo: 1 });
    const msgs = events.filter((e) => e.kind === 'broadcast')
      .map((e) => (e as { message: { type: string } }).message);
    expect(msgs.map((m) => m.type)).toEqual(['subtitle', 'pipeline_warning', 'future_type']);
    gw.close();
  });

  it('response 帧不会误入 broadcast', () => {
    const { gw, events, socket } = makeGateway();
    gw.connect();
    socket().openNow();
    void gw.request('x').catch(() => undefined);
    socket().emit({ type: 'response', id: 'id-1', ok: true, result: null });
    expect(events.filter((e) => e.kind === 'broadcast').length).toBe(0);
    gw.close();
  });

  it('退订后不再收到事件', () => {
    let seq = 0;
    const gw = new Gateway({
      url: 'ws://x', transport, idFactory: () => `i${++seq}`,
      logger: { info: () => undefined, warn: () => undefined, error: () => undefined }
    });
    let count = 0;
    const off = gw.onEvent(() => { count += 1; });
    gw.connect();
    expect(count).toBe(1); // connecting
    off();
    FakeSocket.all[FakeSocket.all.length - 1].openNow();
    FakeSocket.all[FakeSocket.all.length - 1].drop();
    expect(count).toBe(1); // 退订后 open/close 不再回调
    gw.close();
  });
});
