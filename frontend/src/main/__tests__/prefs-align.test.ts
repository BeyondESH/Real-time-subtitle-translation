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
  sourceLanguage: 'ja',
  translationModel: 'hy-mt2-1.8b-q4km',
  audioSource: { kind: 'device', id: '' },
  device: 'auto',
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
    socket.emit({ type: 'response', id: reqFrame?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
  });

  it('源语言一致时不发 set_source_language；档位对齐 change_model 不再发送', async () => {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs({ sourceLanguage: 'ja' }), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    // Whisper 档位对齐随档位移除（client-gateway-state spec）
    expect(socket.sentJson().some((f) => f.action === 'change_model')).toBe(false);
    expect(socket.sentJson().some((f) => f.action === 'set_source_language')).toBe(false);
    expect(socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(false);
  });

  it('源语言不一致时下发 set_source_language', async () => {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs({ sourceLanguage: 'zh' }), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    const frame = socket.sentJson().find((f) => f.action === 'set_source_language');
    expect(frame).toEqual({ type: 'control', action: 'set_source_language', language: 'zh' });
  });

  it('旧后端缺 asr.language → 容错不发 set_source_language', async () => {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs({ sourceLanguage: 'zh' }), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: {} } });
    await p;
    expect(socket.sentJson().some((f) => f.action === 'set_source_language')).toBe(false);
  });

  it('get_config 失败仅告警，不崩溃不阻断音频源对齐', async () => {
    const warns: unknown[][] = [];
    const { gw, socket } = makeOpenGateway(warns);
    const tracker = new AlignmentTracker();
    const p = alignPreferences(gw, prefs({ audioSource: { kind: 'device', id: 'dev-1' } }), tracker, { ...silentLogger, warn: (...a: unknown[]) => { warns.push(a); } });
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: false, error: 'boom' });
    await p;
    expect(warns.length).toBeGreaterThan(0);
    expect(socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(true);
  });

  it('音频源会话级幂等：首次发送，重连不重复；后端重启后重发', async () => {
    const { gw, socket } = makeOpenGateway();
    const tracker = new AlignmentTracker();
    const source = { kind: 'device' as const, id: 'dev-1' };

    // 第一次连接（source 非空）
    let p = alignPreferences(gw, prefs({ audioSource: source }), tracker, silentLogger);
    let req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(1);

    // 模拟 WS 重连（同后端会话）：清空已发帧再对齐 → 不再发送
    socket.sent.length = 0;
    p = alignPreferences(gw, prefs({ audioSource: source }), tracker, silentLogger);
    req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(0);

    // 后端进程重启：tracker 复位后应重新应用
    tracker.markBackendRestarted();
    socket.sent.length = 0;
    p = alignPreferences(gw, prefs({ audioSource: source }), tracker, silentLogger);
    req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(1);
  });

  it('source 为空（默认设备）时永不发送 set_audio_source', async () => {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs({ audioSource: { kind: 'device', id: '' } }), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    expect(socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(false);
  });
});

describe('alignPreferences 音频源结构化对齐（D9）', () => {
  async function run(over: Partial<PrefsSnapshot>, result: unknown) {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs(over), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result });
    await p;
    return { socket };
  }

  const deviceFrame = (id: string): Record<string, unknown> => ({
    type: 'control', action: 'set_audio_source', source: { kind: 'device', id }
  });

  it('后端回传同源（设备）→ 不发 set_audio_source', async () => {
    const { socket } = await run(
      { audioSource: { kind: 'device', id: 'dev-1' } },
      { asr: { model_size: 'base' }, audio: { source: { kind: 'device', id: 'dev-1' } } }
    );
    expect(socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(false);
  });

  it('后端回传不同源（设备）→ 下发 store 结构化源', async () => {
    const { socket } = await run(
      { audioSource: { kind: 'device', id: 'dev-2' } },
      { asr: { model_size: 'base' }, audio: { source: { kind: 'device', id: 'dev-1' } } }
    );
    expect(socket.sentJson()).toContainEqual(deviceFrame('dev-2'));
  });

  it('store 为默认设备而后端为其他设备 → 下发默认设备（device:""）', async () => {
    const { socket } = await run(
      { audioSource: { kind: 'device', id: '' } },
      { asr: { model_size: 'base' }, audio: { source: { kind: 'device', id: 'dev-1' } } }
    );
    expect(socket.sentJson()).toContainEqual(deviceFrame(''));
  });

  it('后端旧形状缺 audio.source → 走 tracker 幂等（非默认设备发送一次）', async () => {
    const { gw, socket } = makeOpenGateway();
    const tracker = new AlignmentTracker();
    const source = { kind: 'device' as const, id: 'dev-1' };
    let p = alignPreferences(gw, prefs({ audioSource: source }), tracker, silentLogger);
    let req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    expect(socket.sentJson()).toContainEqual(deviceFrame('dev-1'));
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(1);

    socket.sent.length = 0;
    p = alignPreferences(gw, prefs({ audioSource: source }), tracker, silentLogger);
    req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language: 'ja' } } });
    await p;
    expect(socket.sentJson().filter((f) => f.action === 'set_audio_source').length).toBe(0);
  });
});

describe('alignPreferences 设备对齐（add-inference-device-toggle D9）', () => {
  const CONFIG_WITH_DEVICE = (
    device: string, resolved: string, reason: string
  ) => ({
    asr: { model_size: 'base', device, resolved_device: resolved, device_reason: reason },
    translation: { resolved_device: resolved, device_reason: reason }
  });

  async function run(over: Partial<PrefsSnapshot>, result: unknown) {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs(over), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result });
    const view = await p;
    return { socket, view };
  }

  it('store 显式 cuda 且与后端偏好不一致 → 下发 change_device', async () => {
    const { socket } = await run({ device: 'cuda' }, CONFIG_WITH_DEVICE('auto', 'cpu', 'no_cuda'));
    expect(socket.sentJson()).toContainEqual({ type: 'control', action: 'change_device', device: 'cuda' });
  });

  it('store 显式值与后端偏好一致 → 不发 change_device', async () => {
    const { socket } = await run({ device: 'cpu' }, CONFIG_WITH_DEVICE('cpu', 'cpu', 'user'));
    expect(socket.sentJson().some((f) => f.action === 'change_device')).toBe(false);
  });

  it('store 为 auto → 不对齐设备（以后端解析为准）', async () => {
    const { socket } = await run({ device: 'auto' }, CONFIG_WITH_DEVICE('cuda', 'cuda', 'auto'));
    expect(socket.sentJson().some((f) => f.action === 'change_device')).toBe(false);
  });

  it('旧后端缺 asr.device → 容错不发送', async () => {
    const { socket } = await run({ device: 'cuda' }, { asr: { model_size: 'base' }, translation: {} });
    expect(socket.sentJson().some((f) => f.action === 'change_device')).toBe(false);
  });

  it('以 get_config 的 resolved_device/device_reason 初始化设备视图', async () => {
    const { view } = await run({ device: 'auto' }, CONFIG_WITH_DEVICE('auto', 'cpu', 'load_failed'));
    expect(view).toEqual({
      asr: { resolved: 'cpu', reason: 'load_failed' },
      translation: { resolved: 'cpu', reason: 'load_failed' }
    });
  });

  it('runtime_failed 原因经 get_config 透传（运行期降级至 CPU）', async () => {
    const { view } = await run({ device: 'auto' }, CONFIG_WITH_DEVICE('auto', 'cpu', 'runtime_failed'));
    expect(view).toEqual({
      asr: { resolved: 'cpu', reason: 'runtime_failed' },
      translation: { resolved: 'cpu', reason: 'runtime_failed' }
    });
  });

  it('旧后端 resolved 字段全缺 → 返回 null（保持检测态）', async () => {
    const { view } = await run({ device: 'auto' }, { asr: { model_size: 'base' }, translation: {} });
    expect(view).toBeNull();
  });
});

describe('alignPreferences 翻译模型对齐（replace-translation-engine-with-llamacpp）', () => {
  async function run(over: Partial<PrefsSnapshot>, result: unknown) {
    const { gw, socket } = makeOpenGateway();
    const p = alignPreferences(gw, prefs(over), new AlignmentTracker(), silentLogger);
    const req = socket.sentJson().find((f) => f.type === 'request');
    socket.emit({ type: 'response', id: req?.id, ok: true, result });
    await p;
    return { socket };
  }

  it('后端回传同模型 → 不发 change_llm', async () => {
    const { socket } = await run(
      {},
      { asr: { model_size: 'base' }, translation: { model: 'hy-mt2-1.8b-q4km' } }
    );
    expect(socket.sentJson().some((f) => f.action === 'change_llm')).toBe(false);
  });

  it('后端回传不同模型 → 下发 store 偏好的 change_llm', async () => {
    const { socket } = await run(
      { translationModel: 'qwen3-1.7b-q4km' },
      { asr: { model_size: 'base' }, translation: { model: 'hy-mt2-1.8b-q4km' } }
    );
    expect(socket.sentJson()).toContainEqual({
      type: 'control', action: 'change_llm', model_id: 'qwen3-1.7b-q4km'
    });
  });

  it('旧后端缺 translation.model → 容错不发送', async () => {
    const { socket } = await run(
      {},
      { asr: { model_size: 'base' }, translation: {} }
    );
    expect(socket.sentJson().some((f) => f.action === 'change_llm')).toBe(false);
  });
});
