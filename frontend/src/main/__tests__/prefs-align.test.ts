/**
 * 偏好对齐单测（client-gateway-state spec "连接建立后的偏好对齐"）
 * 使用真实 Gateway + FakeSocket，验证帧序列与幂等语义
 */
import { describe, it, expect, beforeEach } from 'vitest';
import { Gateway, type WsLike } from '../gateway';
import { alignPreferences, AlignmentTracker, type PrefsSnapshot } from '../prefs-align';

type Handlers = Parameters<WsLike['attach']>[0];

class FakeSocket implements WsLike {
  static all: FakeSocket[] = [];
  readyState = 1; // 直接 OPEN，简化测试
  readonly sent: string[] = [];
  private handlers: Handlers | null = null;

  constructor() {
    FakeSocket.all.push(this);
  }

  attach(h: Handlers): void { this.handlers = h; }
  send(d: string): void { this.sent.push(d); }
  close(): void { this.readyState = 3; }

  emit(msg: unknown): void {
    this.handlers?.message(JSON.stringify(msg));
  }

  sentJson(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }
}

beforeEach(() => { FakeSocket.all = []; });

const silentLogger = { info: () => undefined, warn: () => undefined, error: () => undefined };

function makeOpenGateway(warns: unknown[][] = []) {
  const gw = new Gateway({
    url: 'ws://localhost:8765',
    transport: () => new FakeSocket(),
    logger: { ...silentLogger, warn: (...a: unknown[]) => { warns.push(a); } }
  });
  gw.connect();
  const socket = FakeSocket.all[FakeSocket.all.length - 1];
  return { gw, socket };
}

const prefs = (over: Partial<PrefsSnapshot> = {}): PrefsSnapshot => ({
  targetLanguages: ['zh', 'en'],
  activeLanguage: 'zh',
  model: 'base',
  audioSourceId: '',
  ...over
});

describe('alignPreferences', () => {
  it('config_sync 永远是第一帧', async () => {
    const { gw, socket } = makeOpenGateway();
    socket.emit({ type: 'response', id: 'x', ok: true, result: {} }); // 预热无效
    const p = alignPreferences(gw, prefs(), new AlignmentTracker(), silentLogger);
    const cfgFrame = socket.sentJson().find((f) => f.type === 'config_sync');
    expect(cfgFrame).toEqual({ type: 'config_sync', target_languages: ['zh', 'en'], active_language: 'zh' });
    // 回应 get_config，结束 promise
    const reqFrame = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: reqFrame?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
  });

  it('模型一致时不发 change_model（无差异不发消息）', async () => {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs({ model: 'base' }), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
    expect(socket.sentJson().some((f) => f.action === 'change_model')).toBe(false);
    expect(socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(false);
  });

  it('模型不一致时下发 change_model', async () => {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs({ model: 'small' }), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
    const frame = socket.sentJson().find((f) => f.action === 'change_model');
    expect(frame).toEqual({ type: 'control', action: 'change_model', model_size: 'small' });
  });

  it('get_config 失败仅告警，不崩溃不阻断音频源对齐', async () => {
    const warns: unknown[][] = [];
    const { gw, socket } = makeOpenGateway(warns);
    const tracker = new AlignmentTracker();
    const p = alignPreferences(gw, prefs({ audioSourceId: 'dev-1' }), tracker, { ...silentLogger, warn: (...a: unknown[]) => { warns.push(a); } });
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: false, error: 'boom' });
    await p;
    expect(warns.length).toBeGreaterThan(0);
    expect(socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(true);
  });

  it('音频源会话级幂等：首次发送，重连不重复；后端重启后重发', async () => {
    const { gw, socket } = makeOpenGateway();
    const tracker = new AlignmentTracker();

    // 第一次连接（sourceId 非空）
    let p = alignPreferences(gw, prefs({ audioSourceId: 'dev-1' }), tracker, silentLogger);
    let req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(1);

    // 模拟 WS 重连（同后端会话）：清空已发帧再对齐 → 不再发送
    socket.sent.length = 0;
    p = alignPreferences(gw, prefs({ audioSourceId: 'dev-1' }), tracker, silentLogger);
    req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(0);

    // 后端进程重启：tracker 复位后应重新应用
    tracker.markBackendRestarted();
    socket.sent.length = 0;
    p = alignPreferences(gw, prefs({ audioSourceId: 'dev-1' }), tracker, silentLogger);
    req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(1);
  });

  it('sourceId 为空（默认设备）时永不发送 set_audio_source', async () => {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs({ audioSourceId: '' }), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
    expect(socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(false);
  });
});
