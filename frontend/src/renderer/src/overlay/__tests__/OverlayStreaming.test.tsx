// @vitest-environment jsdom
/**
 * 悬浮窗渐进渲染（add-llm-streaming-output 4.5）：
 * 进行中帧原地更新/新增行且 MUST NOT 推进窗口（5 定稿 + 1 进行中 = 6 行）；
 * 定稿时原地落定并裁剪回 5 行；cancel 移除进行中行；暂停过滤对进行中帧同样生效。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字（white/black），禁止 hex/rgb。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import { OverlayApp } from '../OverlayApp';

type SubtitleCb = (ev: SubtitleStreamEventView) => void;

let subtitleCb: SubtitleCb | null = null;
let patchCb: ((patch: Partial<AppStateView>) => void) | null = null;

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
  patchCb = null;
  window.appAPI = {
    getState: async () => STATE,
    onStatePatch: (cb) => { patchCb = cb; return unsub(); },
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

function finalMsg(id: string, text: string): SubtitleMessageView {
  return {
    type: 'subtitle',
    id,
    original: `orig-${id}`,
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: text }
  };
}

function partialMsg(id: string, text: string): SubtitlePartialMessageView {
  return {
    type: 'subtitle_partial',
    id,
    original: `orig-${id}`,
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: text }
  };
}

async function renderOverlay(): Promise<ReturnType<typeof render>> {
  let utils!: ReturnType<typeof render>;
  await act(async () => { utils = render(<OverlayApp />); });
  return utils;
}

function send(ev: SubtitleStreamEventView): void {
  act(() => { subtitleCb?.(ev); });
}

beforeEach(() => { installApiStub(); });
afterEach(cleanup);

describe('OverlayApp 渐进渲染窗口语义', () => {
  it('5 条定稿 + 1 进行中 = 6 行，且不挤掉任何定稿', async () => {
    const { container } = await renderOverlay();
    act(() => {
      for (let i = 1; i <= 5; i += 1) subtitleCb?.(finalMsg(`f${i}`, `定稿${i}`));
    });
    expect(container.querySelectorAll('.subtitle-line').length).toBe(5);

    send(partialMsg('p1', '进行中'));
    expect(container.querySelectorAll('.subtitle-line').length).toBe(6);
    expect(screen.getByText('定稿1')).not.toBeNull();
    expect(screen.getByText('进行中')).not.toBeNull();
  });

  it('进行中帧原地更新不新增行', async () => {
    const { container } = await renderOverlay();
    send(partialMsg('p1', '进'));
    send(partialMsg('p1', '进行中'));
    expect(container.querySelectorAll('.subtitle-line').length).toBe(1);
    expect(screen.getByText('进行中')).not.toBeNull();
  });

  it('定稿后原地落定并裁剪回 5 行（移除最旧定稿）', async () => {
    const { container } = await renderOverlay();
    act(() => {
      for (let i = 1; i <= 5; i += 1) subtitleCb?.(finalMsg(`f${i}`, `定稿${i}`));
    });
    send(partialMsg('p1', '进行中'));
    send(finalMsg('p1', '定稿p1'));

    expect(container.querySelectorAll('.subtitle-line').length).toBe(5);
    expect(screen.queryByText('定稿1')).toBeNull();
    expect(screen.getByText('定稿p1')).not.toBeNull();
  });

  it('cancel 移除进行中行，既有定稿不受影响', async () => {
    const { container } = await renderOverlay();
    act(() => {
      for (let i = 1; i <= 5; i += 1) subtitleCb?.(finalMsg(`f${i}`, `定稿${i}`));
    });
    send(partialMsg('p1', '进行中'));
    expect(container.querySelectorAll('.subtitle-line').length).toBe(6);

    send({ type: 'subtitle_cancel', id: 'p1' });
    expect(container.querySelectorAll('.subtitle-line').length).toBe(5);
    expect(screen.queryByText('进行中')).toBeNull();
    expect(screen.getByText('定稿1')).not.toBeNull();
  });

  it('暂停期间进行中帧同样被丢弃', async () => {
    const { container } = await renderOverlay();
    act(() => { patchCb?.({ capture: 'paused' }); });
    send(partialMsg('p1', '进行中'));
    expect(container.querySelectorAll('.subtitle-line').length).toBe(0);
  });
});
