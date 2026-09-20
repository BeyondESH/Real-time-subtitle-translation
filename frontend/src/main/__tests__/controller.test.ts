/**
 * Controller 单测：意图执行 / Gateway 广播路由 / 乐观回退 / 历史落库协同
 *
 * 全部依赖用结构替身（type-only 导入 + 对象桩），不加载 electron 与原生模块，
 * 任何 ABI 环境（Node/Electron binding）下均可运行。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { Gateway, type WsLike } from '../gateway';
import { createInitialState, StateStore } from '../state';
import { Controller } from '../controller';
import { AlignmentTracker } from '../prefs-align';
import type { ConfigStore } from '../config';
import type { HistoryStore } from '../history-db';
import type { AppConfig, AudioSourcePref } from '../config-migration';
import { CONFIG_DEFAULTS } from '../config-migration';

// ---------- 替身 ----------

type Handlers = Parameters<WsLike['attach']>[0];

class FakeSocket implements WsLike {
  static all: FakeSocket[] = [];
  readyState = 0;
  readonly sent: string[] = [];
  private handlers: Handlers | null = null;

  constructor() { FakeSocket.all.push(this); }
  attach(h: Handlers): void { this.handlers = h; }
  send(d: string): void { this.sent.push(d); }
  close(): void { this.readyState = 3; }

  openNow(): void { this.readyState = 1; this.handlers?.open(); }
  emit(msg: unknown): void { this.handlers?.message(JSON.stringify(msg)); }
  sentJson(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }
}

function makeConfigStub(): ConfigStore {
  const data: AppConfig = JSON.parse(JSON.stringify(CONFIG_DEFAULTS)) as AppConfig;
  return {
    get: (key: string) => (data as unknown as Record<string, unknown>)[key],
    set: (key: string, value: unknown) => {
      (data as unknown as Record<string, unknown>)[key] = value;
    },
    get all() { return data; }
  } as unknown as ConfigStore;
}

interface HistoryStub {
  store: HistoryStore;
  inserts: Array<Record<string, unknown>>;
  newSessionCount: number;
  armSplit(): void;
}

function makeHistoryStub(): HistoryStub {
  const self: HistoryStub = {
    store: null as unknown as HistoryStore,
    inserts: [],
    newSessionCount: 0,
    armSplit: () => { splitArmed = true; }
  };
  let activeId: string | null = 'sess-0';
  let count = 0;
  let splitArmed = false;

  self.store = {
    getActiveId: () => activeId,
    ensureActiveSession: () => {
      if (!activeId) activeId = 'sess-ensured';
      return activeId;
    },
    newSession: () => {
      self.newSessionCount += 1;
      count = 0;
      activeId = `sess-new-${self.newSessionCount}`;
      return activeId;
    },
    maybeAutoSplit: () => {
      if (!splitArmed) return false;
      splitArmed = false;
      count = 0;
      activeId = 'sess-split';
      return true;
    },
    insertUtterance: (input: Record<string, unknown>) => {
      self.inserts.push(input);
      count += 1;
      return count;
    },
    getSession: () => ({
      id: activeId, title: 't', started_at: 0, ended_at: null,
      audio_source: '', utterance_count: count
    }),
    closeActiveSession: () => { activeId = null; }
  } as unknown as HistoryStore;

  return self;
}

interface Harness {
  controller: Controller;
  state: StateStore;
  socket: FakeSocket;
  broadcasts: Array<{ channel: string; payload: unknown }>;
  config: ConfigStore;
  history: HistoryStub;
  gateway: Gateway;
  onShowSettings: ReturnType<typeof vi.fn>;
  onRestartBackend: ReturnType<typeof vi.fn>;
}

function makeHarness(): Harness {
  FakeSocket.all = [];
  let idSeq = 0;
  const gateway = new Gateway({
    url: 'ws://localhost:8765',
    transport: () => new FakeSocket(),
    logger: { info: () => undefined, warn: () => undefined, error: () => undefined },
    idFactory: () => `id-${++idSeq}`
  });
  const config = makeConfigStub();
  const history = makeHistoryStub();
  const state = new StateStore(createInitialState({
    model: 'base',
    activeLanguage: 'zh',
    targetLanguages: ['zh', 'en'],
    audioSource: '',
    locked: true,
    overlayVisible: true,
    activeSessionId: 'sess-0'
  }));
  const broadcasts: Array<{ channel: string; payload: unknown }> = [];
  const onShowSettings = vi.fn();
  const onRestartBackend = vi.fn();

  const controller = new Controller({
    gateway,
    state,
    config,
    history: history.store,
    tracker: new AlignmentTracker(),
    logger: { info: () => undefined, warn: () => undefined, error: () => undefined },
    broadcast: (channel, payload) => broadcasts.push({ channel, payload }),
    onShowSettings,
    onRestartBackend
  });

  // 连接并就位对齐响应（吸收 open 触发的 align get_config 请求）
  gateway.connect();
  const socket = FakeSocket.all[0];
  socket.openNow();
  const req = socket.sentJson().find((f) => f.type === 'request');
  const language = (config.get('asr') as { language: string }).language;
  socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { language } } });

  return {
    controller, state, socket, broadcasts, config, history, gateway,
    onShowSettings, onRestartBackend
  };
}

const SUBTITLE = {
  type: 'subtitle',
  original: 'こんにちは',
  source_language: 'ja',
  active_language: 'zh',
  translations: { zh: '你好' },
  ts_start: 1.5,
  ts_end: 3.25
};

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); });

describe('广播路由 → 历史落库（session-history spec）', () => {
  it('subtitle：正确映射入库（激活语言译文/时间戳透传）并分发 app:subtitle', () => {
    const h = makeHarness();
    h.socket.emit(SUBTITLE);

    expect(h.history.inserts.length).toBe(1);
    expect(h.history.inserts[0]).toMatchObject({
      original: 'こんにちは',
      sourceLang: 'ja',
      translation: '你好',
      targetLang: 'zh',
      tsStart: 1.5,
      tsEnd: 3.25,
      model: 'Fun-ASR-Nano'
    });
    expect(h.broadcasts.some((b) => b.channel === 'app:subtitle')).toBe(true);
    // 首句 → 会话级刷新广播（供侧栏改题）
    expect(h.broadcasts.some((b) => b.channel === 'history:changed')).toBe(true);
    h.gateway.close();
  });

  it('第二条字幕不再触发会话级广播（避免逐句刷侧栏）', () => {
    const h = makeHarness();
    h.socket.emit(SUBTITLE);
    const before = h.broadcasts.filter((b) => b.channel === 'history:changed').length;
    h.socket.emit({ ...SUBTITLE, translations: { zh: '第二句' } });
    const after = h.broadcasts.filter((b) => b.channel === 'history:changed').length;
    expect(after).toBe(before);
    expect(h.history.inserts.length).toBe(2);
    h.gateway.close();
  });

  it('静音自动切分发生时广播会话级变更', () => {
    const h = makeHarness();
    h.socket.emit(SUBTITLE); // 首句：wasFirst 广播 1 次
    h.history.armSplit();
    h.socket.emit({ ...SUBTITLE, translations: { zh: '切分后' } });
    // 切分句：activeId 变化广播 + (split||wasFirst) 广播 = 2 次，累计 3
    const sessionBroadcasts = h.broadcasts.filter((b) => b.channel === 'history:changed');
    expect(sessionBroadcasts.length).toBe(3);
    expect(h.state.getState().activeSessionId).toBe('sess-split');
    h.gateway.close();
  });

  it('缺激活语言译文时回退原文入库', () => {
    const h = makeHarness();
    h.socket.emit({
      type: 'subtitle', original: 'raw text', source_language: 'en',
      active_language: 'zh', translations: {}
    });
    expect(h.history.inserts[0]).toMatchObject({ translation: 'raw text' });
    h.gateway.close();
  });
});

describe('流式字幕路由（add-llm-streaming-output）', () => {
  const PARTIAL = {
    type: 'subtitle_partial',
    id: 'u42',
    original: 'こんにちは',
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: '你好' }
  };
  const CANCEL = { type: 'subtitle_cancel', id: 'u42', reason: 'dropped' };

  it('subtitle_partial：仅经 app:subtitle 分发，不写历史、不触发会话生命周期', () => {
    const h = makeHarness();
    h.socket.emit(PARTIAL);

    expect(h.history.inserts.length).toBe(0);
    expect(h.broadcasts.some((b) => b.channel === 'history:changed')).toBe(false);
    const subs = h.broadcasts.filter((b) => b.channel === 'app:subtitle');
    expect(subs.length).toBe(1);
    expect(subs[0].payload).toMatchObject({
      type: 'subtitle_partial', id: 'u42', translations: { zh: '你好' }
    });
    h.gateway.close();
  });

  it('subtitle_cancel：仅经 app:subtitle 分发，不写历史、不触发会话生命周期', () => {
    const h = makeHarness();
    h.socket.emit(CANCEL);

    expect(h.history.inserts.length).toBe(0);
    expect(h.broadcasts.some((b) => b.channel === 'history:changed')).toBe(false);
    const subs = h.broadcasts.filter((b) => b.channel === 'app:subtitle');
    expect(subs.length).toBe(1);
    expect(subs[0].payload).toMatchObject({
      type: 'subtitle_cancel', id: 'u42', reason: 'dropped'
    });
    h.gateway.close();
  });

  it('partial → 同 id 定稿：定稿照常落库并广播（既有行为保留，同通道保序）', () => {
    const h = makeHarness();
    h.socket.emit(PARTIAL);
    h.socket.emit({ ...SUBTITLE, id: 'u42', first_token_ms: 312 });

    expect(h.history.inserts.length).toBe(1);
    expect(h.history.inserts[0]).toMatchObject({
      original: 'こんにちは', translation: '你好', tsStart: 1.5, tsEnd: 3.25
    });
    const subs = h.broadcasts.filter((b) => b.channel === 'app:subtitle');
    expect(subs.length).toBe(2);
    expect(subs[0].payload).toMatchObject({ type: 'subtitle_partial', id: 'u42' });
    expect(subs[1].payload).toMatchObject({
      type: 'subtitle', id: 'u42', first_token_ms: 312
    });
    h.gateway.close();
  });
});

describe('意图执行（client-gateway-state spec：单一动作源）', () => {
  it('togglePause：WS 帧 + 状态迁移双向一致，再次触发为 resume', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'togglePause' });
    expect(h.state.getState().capture).toBe('paused');
    expect(h.socket.sentJson().some((f) => f.action === 'pause')).toBe(true);
    h.controller.handle({ type: 'togglePause' });
    expect(h.state.getState().capture).toBe('running');
    expect(h.socket.sentJson().some((f) => f.action === 'resume')).toBe(true);
    h.gateway.close();
  });

  it('setSourceLanguage：持久化 + 下发 set_source_language（热生效）', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setSourceLanguage', language: 'zh' });
    expect((h.config.get('asr') as { language: string }).language).toBe('zh');
    expect(h.socket.sentJson()).toContainEqual({
      type: 'control', action: 'set_source_language', language: 'zh'
    });
    h.gateway.close();
  });

  it('setSourceLanguage：同值幂等；范围外语言拒绝（无 WS 帧）', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setSourceLanguage', language: 'ja' }); // 默认值 → 幂等
    expect(h.socket.sentJson().some((f) => f.action === 'set_source_language')).toBe(false);

    // 运行时守卫验证：类型层排除的范围外值（模拟渲染层被绕过/后端旧版本）
    h.controller.handle({ type: 'setSourceLanguage', language: 'ko' } as unknown as Parameters<
      typeof h.controller.handle
    >[0]);
    expect(h.socket.sentJson().some((f) => f.action === 'set_source_language')).toBe(false);
    expect((h.config.get('asr') as { language: string }).language).toBe('ja');
    h.gateway.close();
  });

  it('cycleLanguage：轮换激活语言并持久化', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'cycleLanguage' });
    expect(h.state.getState().activeLanguage).toBe('en');
    const t = h.config.get('translation') as { activeLanguage: string };
    expect(t.activeLanguage).toBe('en');
    expect(h.socket.sentJson()).toContainEqual({
      type: 'control', action: 'set_language', language: 'en'
    });
    h.gateway.close();
  });

  it('setLanguage 拒绝目标列表外语言：仅错误 toast，无 WS 帧、状态不变', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setLanguage', language: 'fr' });
    expect(h.state.getState().activeLanguage).toBe('zh');
    expect(h.socket.sentJson().some((f) => f.action === 'set_language')).toBe(false);
    const errors = (h.broadcasts.filter((b) => b.channel === 'app:toast') as
      Array<{ payload: { kind: string } }>).filter((t) => t.payload.kind === 'error');
    expect(errors.length).toBe(1);
    h.gateway.close();
  });

  it('setLlm：同值幂等；新值下发 change_llm 并持久化', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setLlm', modelId: 'hy-mt2-1.8b-q4km' }); // 默认值 → 幂等
    expect(h.socket.sentJson().some((f) => f.action === 'change_llm')).toBe(false);

    h.controller.handle({ type: 'setLlm', modelId: 'qwen3-1.7b-q4km' });
    expect(h.socket.sentJson()).toContainEqual({
      type: 'control', action: 'change_llm', model_id: 'qwen3-1.7b-q4km'
    });
    expect((h.config.get('translation') as { model: string }).model).toBe('qwen3-1.7b-q4km');
    h.gateway.close();
  });

  it('setLlm：空 modelId 拒绝（仅错误 toast，无 WS 帧）', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setLlm', modelId: '' });
    expect(h.socket.sentJson().some((f) => f.action === 'change_llm')).toBe(false);
    const errors = (h.broadcasts.filter((b) => b.channel === 'app:toast') as
      Array<{ payload: { kind: string } }>).filter((t) => t.payload.kind === 'error');
    expect(errors.length).toBe(1);
    h.gateway.close();
  });

  it('toggleLock / toggleOverlay：状态迁移 + locked 持久化', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'toggleLock' });
    expect(h.state.getState().locked).toBe(false);
    expect(h.config.get('locked')).toBe(false);
    h.controller.handle({ type: 'toggleOverlay' });
    expect(h.state.getState().overlayVisible).toBe(false);
    h.gateway.close();
  });

  it('setAudioSource：同值幂等跳过，新值三处同步（结构化设备源）', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setAudioSource', source: { kind: 'device', id: '' } });
    expect(h.socket.sentJson().some((f) => f.action === 'set_audio_source')).toBe(false);
    h.controller.handle({ type: 'setAudioSource', source: { kind: 'device', id: 'dev-1' } });
    expect(h.socket.sentJson()).toContainEqual({
      type: 'control', action: 'set_audio_source', source: { kind: 'device', id: 'dev-1' }
    });
    expect(h.state.getState().audioSource).toBe('dev-1');
    expect((h.config.get('audio') as { source: AudioSourcePref }).source)
      .toEqual({ kind: 'device', id: 'dev-1' });
    h.gateway.close();
  });

  it('newSession：历史库开新会话 + 状态切换 + 会话级广播', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'newSession' });
    expect(h.history.newSessionCount).toBe(1);
    expect(h.state.getState().activeSessionId).toBe('sess-new-1');
    expect(h.broadcasts.some((b) => b.channel === 'history:changed')).toBe(true);
    h.gateway.close();
  });

  it('showSettings / restartBackend 走注入回调', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'showSettings' });
    h.controller.handle({ type: 'restartBackend' });
    expect(h.onShowSettings).toHaveBeenCalledTimes(1);
    expect(h.onRestartBackend).toHaveBeenCalledTimes(1);
    h.gateway.close();
  });
});

describe('乐观回退（settings-management spec：失败回退原值）', () => {
  it('invalid_language 回执（源语言路径）→ asr 配置回退原值 + 错误 toast', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setSourceLanguage', language: 'zh' });
    expect((h.config.get('asr') as { language: string }).language).toBe('zh');
    h.socket.emit({ type: 'error', code: 'invalid_language', message: '不支持的源语言' });
    expect((h.config.get('asr') as { language: string }).language).toBe('ja');
    const errors = (h.broadcasts.filter((b) => b.channel === 'app:toast') as
      Array<{ payload: { kind: string; text: string } }>).filter((t) => t.payload.kind === 'error');
    expect(errors.some((t) => t.payload.text.includes('不支持的源语言'))).toBe(true);
    h.gateway.close();
  });

  it('invalid_model 回执：单引擎语义下无乐观锚点，不破坏状态', () => {
    const h = makeHarness();
    h.socket.emit({ type: 'error', code: 'invalid_model', message: '不支持的模型' });
    // 源语言配置不受影响（档位已移除，无回退路径）
    expect((h.config.get('asr') as { language: string }).language).toBe('ja');
    h.gateway.close();
  });

  it('invalid_audio_source 回执（用户切换）→ 音频源回退原值', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setAudioSource', source: { kind: 'device', id: 'dev-x' } });
    expect(h.state.getState().audioSource).toBe('dev-x');
    h.socket.emit({ type: 'error', code: 'invalid_audio_source', message: 'bad' });
    expect(h.state.getState().audioSource).toBe('');
    expect((h.config.get('audio') as { source: AudioSourcePref }).source)
      .toEqual({ kind: 'device', id: '' });
    h.gateway.close();
  });

  it('翻译模型失败回执（下载失败）→ store 回退原值 + 错误 toast', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setLlm', modelId: 'qwen3-1.7b-q4km' });
    expect((h.config.get('translation') as { model: string }).model).toBe('qwen3-1.7b-q4km');

    h.socket.emit({ type: 'error', code: 'model_download_failed', message: 'net down' });

    expect((h.config.get('translation') as { model: string }).model).toBe('hy-mt2-1.8b-q4km');
    const errors = (h.broadcasts.filter((b) => b.channel === 'app:toast') as
      Array<{ payload: { kind: string; text: string } }>).filter((t) => t.payload.kind === 'error');
    expect(errors.some((t) => t.payload.text.includes('net down'))).toBe(true);
    h.gateway.close();
  });

  it('翻译模型非法回执（invalid_llm / llm_load_failed）→ 同样回退', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setLlm', modelId: 'ghost-model' });
    h.socket.emit({ type: 'error', code: 'invalid_llm', message: 'nope' });
    expect((h.config.get('translation') as { model: string }).model).toBe('hy-mt2-1.8b-q4km');

    h.controller.handle({ type: 'setLlm', modelId: 'hy-mt2-7b-q4km' });
    h.socket.emit({ type: 'error', code: 'llm_load_failed', message: 'spawn failed' });
    expect((h.config.get('translation') as { model: string }).model).toBe('hy-mt2-1.8b-q4km');
    h.gateway.close();
  });

  it('invalid_audio_source 回执（对齐路径）→ 粘性重置默认设备 + 单条 warn toast', async () => {
    const h = makeHarness();
    // store 记录非默认设备 → 对齐下发 → 后端拒绝（无用户乐观锚点）
    h.config.set('audio', { source: { kind: 'device', id: 'dev-x' } });
    const p = h.controller.align();
    const req = h.socket.sentJson().filter((f) => f.type === 'request').pop();
    h.socket.emit({ type: 'response', id: req?.id, ok: true, result: { asr: { model_size: 'base' } } });
    await p;
    expect(h.socket.sentJson()).toContainEqual({
      type: 'control', action: 'set_audio_source',
      source: { kind: 'device', id: 'dev-x' }
    });

    h.socket.emit({ type: 'error', code: 'invalid_audio_source', message: '设备不可用' });
    expect(h.state.getState().audioSource).toBe('');
    expect((h.config.get('audio') as { source: AudioSourcePref }).source)
      .toEqual({ kind: 'device', id: '' });
    const audioToasts = (h.broadcasts.filter((b) => b.channel === 'app:toast') as
      Array<{ payload: { kind: string; text: string } }>)
      .filter((t) => t.payload.text.includes('音频源不可用'));
    expect(audioToasts.length).toBe(1);
    expect(audioToasts[0].payload.kind).toBe('warn');
    h.gateway.close();
  });

  it('invalid_language 回执 → 激活语言回退', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setLanguage', language: 'en' });
    h.socket.emit({ type: 'error', code: 'invalid_language', message: 'bad' });
    expect(h.state.getState().activeLanguage).toBe('zh');
    h.gateway.close();
  });
});

describe('其它广播路由', () => {
  it('model_progress → modelDownload；100% 后 3s 清空', () => {
    const h = makeHarness();
    h.socket.emit({ type: 'model_progress', model_name: 'whisper-base', progress: 50, message: 'downloading' });
    expect(h.state.getState().modelDownload).toEqual({
      name: 'whisper-base', progress: 50, message: 'downloading'
    });
    h.socket.emit({ type: 'model_progress', model_name: 'whisper-base', progress: 100, message: 'done' });
    expect(h.state.getState().modelDownload).not.toBeNull();
    vi.advanceTimersByTime(3000);
    expect(h.state.getState().modelDownload).toBeNull();
    h.gateway.close();
  });

  it('pipeline_warning → droppedCount/lastWarning + warn toast', () => {
    const h = makeHarness();
    h.socket.emit({ type: 'pipeline_warning', reason: 'queue_full', dropped: 3, message: '过载' });
    expect(h.state.getState().droppedCount).toBe(3);
    expect(h.state.getState().lastWarning?.droppedTotal).toBe(3);
    const warns = (h.broadcasts.filter((b) => b.channel === 'app:toast') as
      Array<{ payload: { kind: string; text: string } }>).filter((t) => t.payload.kind === 'warn');
    expect(warns.length).toBe(1);
    expect(warns[0].payload.text).toBe('过载');
    h.gateway.close();
  });

  it('pipeline_warning 新 reason 透传字段（stalled/engine_degraded，含 pending/detail）', () => {
    const h = makeHarness();
    h.socket.emit({
      type: 'pipeline_warning',
      reason: 'stalled',
      dropped: 5,
      pending: 3,
      message: '识别引擎停滞，正在自动恢复…',
      detail: { engine: 'asr' }
    });
    expect(h.state.getState().lastWarning).toMatchObject({
      droppedTotal: 5,
      reason: 'stalled',
      pending: 3,
      message: '识别引擎停滞，正在自动恢复…',
      detail: { engine: 'asr' }
    });

    h.socket.emit({
      type: 'pipeline_warning',
      reason: 'engine_degraded',
      dropped: 5,
      message: '当前模型在 CPU 上难以实时，建议切换到 base/small',
      detail: { engine: 'asr', from: 'cuda', to: 'cpu', device_reason: 'runtime_failed' }
    });
    const w = h.state.getState().lastWarning;
    expect(w?.reason).toBe('engine_degraded');
    expect(w?.detail).toEqual({
      engine: 'asr', from: 'cuda', to: 'cpu', device_reason: 'runtime_failed'
    });
    h.gateway.close();
  });

  it('pipeline_warning 未知 reason / 缺 dropped：防御性容错不抛错', () => {
    const h = makeHarness();
    expect(() => h.socket.emit({
      type: 'pipeline_warning', reason: 'future_reason', message: 'x'
    })).not.toThrow();
    const w = h.state.getState().lastWarning;
    expect(w?.reason).toBe('future_reason');
    expect(w?.droppedTotal).toBe(1);
    h.gateway.close();
  });

  it('vad_state 合法值迁移，非法值忽略；未知广播类型安全', () => {
    const h = makeHarness();
    h.socket.emit({ type: 'vad_state', state: 'speech' });
    expect(h.state.getState().vad).toBe('speech');
    h.socket.emit({ type: 'vad_state', state: 'bogus' });
    expect(h.state.getState().vad).toBe('speech');
    expect(() => h.socket.emit({ type: 'future_type', x: 1 })).not.toThrow();
    h.gateway.close();
  });
});

describe('推理设备（add-inference-device-toggle D9）', () => {
  const DEVICE_STATE_CPU = {
    type: 'device_state',
    asr: { resolved: 'cpu', reason: 'no_cuda' },
    translation: { resolved: 'cpu', reason: 'no_cuda' }
  };

  it('setDevice：下发 change_device + 落盘 + 切换中置 device=null', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setDevice', device: 'cuda' });
    expect(h.socket.sentJson()).toContainEqual({
      type: 'control', action: 'change_device', device: 'cuda'
    });
    expect((h.config.get('inference') as { device: string }).device).toBe('cuda');
    expect(h.state.getState().device).toBeNull();
    h.gateway.close();
  });

  it('setDevice 同值幂等：不发帧、不改状态', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setDevice', device: 'auto' });
    expect(h.socket.sentJson().some((f) => f.action === 'change_device')).toBe(false);
    h.gateway.close();
  });

  it('device_state 广播 → 更新 AppState.device（含降级 cpu+load_failed，无错误 toast）', () => {
    const h = makeHarness();
    h.controller.handle({ type: 'setDevice', device: 'cuda' });
    const toastsBefore = h.broadcasts.filter((b) => b.channel === 'app:toast').length;
    h.socket.emit({
      type: 'device_state',
      asr: { resolved: 'cpu', reason: 'load_failed' },
      translation: { resolved: 'cpu', reason: 'no_cuda' }
    });
    expect(h.state.getState().device).toEqual({
      asr: { resolved: 'cpu', reason: 'load_failed' },
      translation: { resolved: 'cpu', reason: 'no_cuda' }
    });
    // 静默降级：select 保持用户选择，不视为失败
    expect((h.config.get('inference') as { device: string }).device).toBe('cuda');
    expect(h.broadcasts.filter((b) => b.channel === 'app:toast').length).toBe(toastsBefore);
    h.gateway.close();
  });

  it('device_state reason=runtime_failed → 更新设备视图（运行期降级，无错误 toast）', () => {
    const h = makeHarness();
    const toastsBefore = h.broadcasts.filter((b) => b.channel === 'app:toast').length;
    h.socket.emit({
      type: 'device_state',
      asr: { resolved: 'cpu', reason: 'runtime_failed' },
      translation: { resolved: 'cpu', reason: 'no_cuda' }
    });
    expect(h.state.getState().device).toEqual({
      asr: { resolved: 'cpu', reason: 'runtime_failed' },
      translation: { resolved: 'cpu', reason: 'no_cuda' }
    });
    expect(h.broadcasts.filter((b) => b.channel === 'app:toast').length).toBe(toastsBefore);
    h.gateway.close();
  });

  it('device_state resolved=null → 检测态（device 置 null）', () => {
    const h = makeHarness();
    h.socket.emit(DEVICE_STATE_CPU);
    expect(h.state.getState().device).not.toBeNull();
    h.socket.emit({
      type: 'device_state',
      asr: { resolved: null, reason: 'auto' },
      translation: { resolved: null, reason: 'auto' }
    });
    expect(h.state.getState().device).toBeNull();
    h.gateway.close();
  });

  it('invalid_device 回执 → 偏好与设备视图回退 + 错误 toast', () => {
    const h = makeHarness();
    h.socket.emit(DEVICE_STATE_CPU);
    h.controller.handle({ type: 'setDevice', device: 'cuda' });
    h.socket.emit({ type: 'error', code: 'invalid_device', message: '不支持的推理设备' });
    expect((h.config.get('inference') as { device: string }).device).toBe('auto');
    expect(h.state.getState().device).toEqual({
      asr: { resolved: 'cpu', reason: 'no_cuda' },
      translation: { resolved: 'cpu', reason: 'no_cuda' }
    });
    const errors = (h.broadcasts.filter((b) => b.channel === 'app:toast') as
      Array<{ payload: { kind: string; text: string } }>).filter((t) => t.payload.kind === 'error');
    expect(errors.some((t) => t.payload.text.includes('不支持的推理设备'))).toBe(true);
    h.gateway.close();
  });

  it('旧后端无 device_state 广播 + get_config 无设备字段 → device 保持 null', () => {
    const h = makeHarness(); // 对齐响应仅含 model_size
    expect(h.state.getState().device).toBeNull();
    h.gateway.close();
  });
});
