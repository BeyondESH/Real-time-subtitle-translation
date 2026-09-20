// @vitest-environment jsdom
/**
 * useSubtitleStream 按 id upsert（add-llm-streaming-output 4.5）：
 * partial 只建一条且同 id 原地更新（seq 稳定、无重复卡片）、final 原地替换并清 streaming、
 * cancel 移除进行中条目、无 partial 的 final（含旧后端无 id）容错追加、cap 仍生效。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字，禁止 hex/rgb。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { useSubtitleStream, type CaptionEntry } from '../hooks';

type SubtitleCb = (ev: SubtitleStreamEventView) => void;

let subtitleCb: SubtitleCb | null = null;

const unsub = (): (() => void) => () => undefined;

const CONFIG: AppConfigView = {
  window: {
    width: 800, height: 200, x: null, y: null, opacity: 0.9,
    displayId: null, positions: {}
  },
  subtitle: {
    fontFamily: 'Microsoft YaHei',
    fontSize: 24,
    fontColor: 'white',
    strokeColor: 'black',
    strokeWidth: 2,
    displayMode: 'original_and_translation',
    preset: 'text'
  },
  websocket: { host: 'localhost', port: 8765 },
  shortcuts: {
    togglePause: 'Ctrl+Shift+Space',
    switchLanguage: 'Ctrl+Shift+L',
    toggleLock: 'Ctrl+Shift+D'
  },
  shortcutStatus: { togglePause: true, switchLanguage: true, toggleLock: true },
  translation: { targetLanguages: ['zh', 'en'], activeLanguage: 'zh', model: 'hy-mt2-1.8b-q4km' },
  asr: { language: 'ja' },
  inference: { device: 'auto' },
  audio: { source: { kind: 'device', id: '' } },
  locked: true,
  theme: 'dark',
  system: { autoStart: false },
  sessions: { autoSplitSilenceMin: null },
  ui: { sidebarCollapsed: false, mainWindow: { width: 1080, height: 720, x: null, y: null } },
  onboarding: { completed: true }
};

const STATE: AppStateView = {
  connection: 'open',
  capture: 'running',
  vad: 'silence',
  model: 'base',
  modelDownload: null,
  activeLanguage: 'zh',
  targetLanguages: ['zh', 'en'],
  audioSource: '',
  locked: true,
  overlayVisible: true,
  lastWarning: null,
  droppedCount: 0,
  device: null,
  activeSessionId: null
};

function installApiStub(): void {
  subtitleCb = null;
  window.appAPI = {
    getState: async () => STATE,
    onStatePatch: unsub,
    getConfig: async () => CONFIG,
    onConfigChanged: unsub,
    onSubtitle: (cb) => { subtitleCb = cb; return unsub(); },
    onToast: unsub,
    dispatch: async () => undefined,
    wsRequest: async () => ({ ok: true, result: [] }),
    getEnv: async () => ({
      platform: 'win32', supportsWco: true, supportsAcrylic: true, version: '2.0.0'
    }),
    setConfig: async () => true,
    onNavigate: unsub,
    openPath: async () => true,
    checkUpdate: async () => ({ phase: 'not-available' }),
    onUpdate: unsub,
    quitAndInstall: async () => undefined,
    getDisplays: async () => [],
    listSessions: async () => [],
    getSession: async () => null,
    searchHistory: async () => [],
    renameSession: async () => true,
    deleteSession: async () => ({ deleted: true, wasActive: false }),
    newSession: async () => null,
    historyStats: async () => ({ sizeBytes: 0, sessionCount: 0, utteranceCount: 0 }),
    clearHistory: async () => ({ sizeBytes: 0, sessionCount: 0, utteranceCount: 0 }),
    exportSession: async () => ({ ok: true }),
    onHistoryChanged: unsub
  };
}

function partial(id: string, text: string): SubtitlePartialMessageView {
  return {
    type: 'subtitle_partial',
    id,
    original: '今日は天気がいいですね',
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: text }
  };
}

function finalEvent(id: string | undefined, text: string): SubtitleMessageView {
  const msg: SubtitleMessageView = {
    type: 'subtitle',
    original: '今日は天気がいいですね',
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: text }
  };
  if (id !== undefined) msg.id = id;
  return msg;
}

function translationOf(entry: CaptionEntry): string {
  const m = entry.msg;
  const active = m.active_language;
  return (active && m.translations && m.translations[active]) || m.original;
}

beforeEach(() => { installApiStub(); });
afterEach(cleanup);

describe('useSubtitleStream 按 id upsert', () => {
  it('partial 只建一条；同 id 二次 partial 原地更新（seq 不变）', () => {
    const { result } = renderHook(() => useSubtitleStream('s1', 500));

    act(() => { subtitleCb?.(partial('u1', '你好')); });
    expect(result.current).toHaveLength(1);
    expect(result.current[0].seq).toBe(1);
    expect(result.current[0].streaming).toBe(true);
    expect(result.current[0].msg.type).toBe('subtitle_partial');

    act(() => { subtitleCb?.(partial('u1', '你好世界')); });
    expect(result.current).toHaveLength(1);
    expect(result.current[0].seq).toBe(1);
    expect(translationOf(result.current[0])).toBe('你好世界');
  });

  it('final 同 id 原地替换并清 streaming（seq 不变）', () => {
    const { result } = renderHook(() => useSubtitleStream('s1', 500));

    act(() => { subtitleCb?.(partial('u1', '你好')); });
    const seq = result.current[0].seq;

    act(() => { subtitleCb?.(finalEvent('u1', '你好世界')); });
    expect(result.current).toHaveLength(1);
    expect(result.current[0].seq).toBe(seq);
    expect(result.current[0].streaming).toBe(false);
    expect(result.current[0].msg.type).toBe('subtitle');
    expect(translationOf(result.current[0])).toBe('你好世界');
  });

  it('cancel 移除进行中条目', () => {
    const { result } = renderHook(() => useSubtitleStream('s1', 500));

    act(() => { subtitleCb?.(partial('u1', '你好')); });
    expect(result.current).toHaveLength(1);

    act(() => { subtitleCb?.({ type: 'subtitle_cancel', id: 'u1' }); });
    expect(result.current).toHaveLength(0);
  });

  it('cancel 不影响已定稿条目', () => {
    const { result } = renderHook(() => useSubtitleStream('s1', 500));

    act(() => { subtitleCb?.(finalEvent('u1', '你好')); });
    act(() => { subtitleCb?.({ type: 'subtitle_cancel', id: 'u1' }); });
    expect(result.current).toHaveLength(1);
  });

  it('无 partial 的 final 容错追加（含旧后端无 id）', () => {
    const { result } = renderHook(() => useSubtitleStream('s1', 500));

    act(() => { subtitleCb?.(finalEvent('u9', '甲')); });
    act(() => { subtitleCb?.(finalEvent(undefined, '乙')); });
    expect(result.current).toHaveLength(2);
    expect(result.current.every((e) => e.streaming === false)).toBe(true);
  });

  it('cap 仍生效（内存滚动窗口）', () => {
    const { result } = renderHook(() => useSubtitleStream('s1', 2));

    act(() => { subtitleCb?.(finalEvent('a', '1')); });
    act(() => { subtitleCb?.(finalEvent('b', '2')); });
    act(() => { subtitleCb?.(finalEvent('c', '3')); });
    expect(result.current).toHaveLength(2);
    expect(result.current.map(translationOf)).toEqual(['2', '3']);
  });

  it('resetKey 变化清流', () => {
    const { result, rerender } = renderHook(
      ({ k }: { k: string | null }) => useSubtitleStream(k, 500),
      { initialProps: { k: 's1' as string | null } }
    );

    act(() => { subtitleCb?.(finalEvent('a', '1')); });
    expect(result.current).toHaveLength(1);

    rerender({ k: 's2' });
    expect(result.current).toHaveLength(0);
  });
});
