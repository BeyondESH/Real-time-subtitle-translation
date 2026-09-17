/**
 * AppState reducer + StateStore 合帧广播单测
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { createInitialState, reduce, StateStore, type AppState, type AppAction } from '../state';

function base(overrides: Partial<AppState> = {}): AppState {
  return createInitialState({
    model: 'base',
    activeLanguage: 'zh',
    targetLanguages: ['zh', 'en'],
    audioSource: '',
    locked: true,
    overlayVisible: true,
    ...overrides
  });
}

describe('reduce（纯函数）', () => {
  it('无变化的 action 返回同一引用', () => {
    const s = base();
    expect(reduce(s, { type: 'connectionChanged', state: s.connection })).toBe(s);
    expect(reduce(s, { type: 'modelChanged', model: 'base' })).toBe(s);
    expect(reduce(s, { type: 'targetLanguagesChanged', targetLanguages: ['zh', 'en'] })).toBe(s);
    expect(reduce(s, { type: 'vadChanged', state: 'speech' }) === s).toBe(false);
    expect(reduce(s, { type: 'lockToggled', locked: true })).toBe(s);
  });

  it('connectionChanged / modelChanged / audioSourceChanged', () => {
    const s1 = reduce(base(), { type: 'connectionChanged', state: 'open' });
    expect(s1.connection).toBe('open');
    const s2 = reduce(s1, { type: 'modelChanged', model: 'small' });
    expect(s2.model).toBe('small');
    const s3 = reduce(s2, { type: 'audioSourceChanged', audioSource: 'dev-1' });
    expect(s3.audioSource).toBe('dev-1');
  });

  it('captureToggled 无参取反，带参显式设置', () => {
    let s = base();
    s = reduce(s, { type: 'captureToggled' });
    expect(s.capture).toBe('paused');
    s = reduce(s, { type: 'captureToggled' });
    expect(s.capture).toBe('running');
    s = reduce(s, { type: 'captureToggled', paused: true });
    expect(s.capture).toBe('paused');
    s = reduce(s, { type: 'captureToggled', paused: true });
    expect(s.capture).toBe('paused');
  });

  it('lockToggled / overlayVisibilityChanged', () => {
    const s = reduce(base(), { type: 'lockToggled' });
    expect(s.locked).toBe(false);
    const s2 = reduce(s, { type: 'overlayVisibilityChanged', visible: false });
    expect(s2.overlayVisible).toBe(false);
  });

  it('modelDownloadProgress 更新，phase 完成后 Done 清空', () => {
    let s = reduce(base(), { type: 'modelDownloadProgress', name: 'whisper-base', progress: 40, message: 'downloading' });
    expect(s.modelDownload).toEqual({ name: 'whisper-base', progress: 40, message: 'downloading' });
    const same = reduce(s, { type: 'modelDownloadProgress', name: 'whisper-base', progress: 40, message: 'downloading' });
    expect(same).toBe(s); // 完全相同进度 → 同一引用
    s = reduce(s, { type: 'modelDownloadDone' });
    expect(s.modelDownload).toBeNull();
  });

  it('targetLanguagesChanged 维持激活语言不变式（不在列表则回退首项）', () => {
    const s = reduce(base({ activeLanguage: 'en' }), { type: 'targetLanguagesChanged', targetLanguages: ['ja', 'zh'] });
    expect(s.activeLanguage).toBe('ja');
    const s2 = reduce(base({ activeLanguage: 'zh' }), { type: 'targetLanguagesChanged', targetLanguages: ['zh', 'ja'] });
    expect(s2.activeLanguage).toBe('zh');
  });

  it('pipelineWarning：droppedTotal 采用后端累计值，缺省时本地 +1', () => {
    const s = reduce(base(), { type: 'pipelineWarning', droppedTotal: 7, at: 111 });
    expect(s.droppedCount).toBe(7);
    expect(s.lastWarning).toEqual({ droppedTotal: 7, at: 111 });
    const s2 = reduce(s, { type: 'pipelineWarning' });
    expect(s2.droppedCount).toBe(8);
    expect(s2.lastWarning?.at).toBeTypeOf('number');
  });

  it('configApplied 合并 patch', () => {
    const s = reduce(base(), { type: 'configApplied', patch: { model: 'tiny', locked: false } });
    expect(s.model).toBe('tiny');
    expect(s.locked).toBe(false);
  });

  it('三源一致：同一 base + 同一 action 多次 reduce 结果深相等（纯函数）', () => {
    const s = base();
    const action: AppAction = { type: 'captureToggled' };
    const a = reduce(s, action);
    const b = reduce(s, action);
    const c = reduce(s, action);
    expect(a).toEqual(b);
    expect(b).toEqual(c);
    expect(s.capture).toBe('running'); // base 不被改动
  });
});

describe('StateStore（合帧广播）', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('16ms 内多次 dispatch 合并为一次 patch 回调', () => {
    const store = new StateStore(base());
    const calls: Array<Partial<AppState>> = [];
    store.subscribe((patch) => calls.push(patch));

    store.dispatch({ type: 'connectionChanged', state: 'open' });
    store.dispatch({ type: 'captureToggled', paused: true });
    store.dispatch({ type: 'modelChanged', model: 'small' });
    expect(calls.length).toBe(0); // 未到期不广播

    vi.advanceTimersByTime(16);
    expect(calls.length).toBe(1);
    expect(calls[0]).toEqual({ connection: 'open', capture: 'paused', model: 'small' });
    expect(store.getState().connection).toBe('open');
  });

  it('跨批次 dispatch 分别广播；patch 只含变化键', () => {
    const store = new StateStore(base());
    const calls: Array<Partial<AppState>> = [];
    store.subscribe((patch) => calls.push(patch));

    store.dispatch({ type: 'vadChanged', state: 'speech' });
    vi.advanceTimersByTime(16);
    store.dispatch({ type: 'vadChanged', state: 'silence' });
    vi.advanceTimersByTime(16);

    expect(calls.length).toBe(2);
    expect(Object.keys(calls[0])).toEqual(['vad']);
    expect(calls[1]).toEqual({ vad: 'silence' });
  });

  it('no-op dispatch 不产生广播', () => {
    const store = new StateStore(base());
    let called = 0;
    store.subscribe(() => { called += 1; });
    store.dispatch({ type: 'modelChanged', model: 'base' }); // 与当前相同
    vi.advanceTimersByTime(100);
    expect(called).toBe(0);
  });

  it('同键连续变更取最终值', () => {
    const store = new StateStore(base());
    const calls: Array<Partial<AppState>> = [];
    store.subscribe((p) => calls.push(p));
    store.dispatch({ type: 'vadChanged', state: 'speech' });
    store.dispatch({ type: 'vadChanged', state: 'silence' });
    vi.advanceTimersByTime(16);
    expect(calls.length).toBe(1);
    expect(calls[0]).toEqual({ vad: 'silence' });
  });

  it('flushNow 立即冲刷，退订后不再回调', () => {
    const store = new StateStore(base());
    let a = 0;
    let b = 0;
    const offB = store.subscribe(() => { b += 1; });
    store.subscribe(() => { a += 1; });
    store.dispatch({ type: 'connectionChanged', state: 'open' });
    offB();
    store.flushNow();
    expect(a).toBe(1);
    expect(b).toBe(0);
  });
});
