// @vitest-environment jsdom
/**
 * 悬浮窗告警细条（subtitle-display spec「过载告警可视化」）：
 * 按 reason 文案、未知 reason 泛化容错、连续告警不堆叠、约 2s 自动消失。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字（white/black），禁止 hex/rgb。
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup, act } from '@testing-library/react';
import { OverlayApp, warningStripText } from '../OverlayApp';

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
    switchModel: 'Ctrl+Shift+M',
    toggleLock: 'Ctrl+Shift+D'
  },
  shortcutStatus: {
    togglePause: true, switchLanguage: true, switchModel: true, toggleLock: true
  },
    translation: { targetLanguages: ['zh', 'en'], activeLanguage: 'zh', model: 'hy-mt2-1.8b-q4km' },
  asr: { model: 'base' },
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

const unsub = (): (() => void) => () => undefined;

let patchCb: ((patch: Partial<AppStateView>) => void) | null = null;

function installApiStub(): void {
  patchCb = null;
  window.appAPI = {
    getState: async () => STATE,
    onStatePatch: (cb) => { patchCb = cb; return unsub(); },
    getConfig: async () => CONFIG,
    onConfigChanged: unsub,
    onSubtitle: unsub,
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

async function renderOverlay(): Promise<ReturnType<typeof render>> {
  let utils!: ReturnType<typeof render>;
  await act(async () => { utils = render(<OverlayApp />); });
  return utils;
}

beforeEach(() => { installApiStub(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe('warningStripText（按 reason 文案）', () => {
  it('queue_full：过载文案 + 累计丢弃数', () => {
    expect(warningStripText({ droppedTotal: 3, at: 0, reason: 'queue_full' }))
      .toBe('处理过载 · 累计丢弃 3 句（建议切换更小的模型）');
  });

  it('stalled：优先后端 message，缺失时回退', () => {
    expect(warningStripText({
      droppedTotal: 2, at: 0, reason: 'stalled', message: '识别引擎停滞，正在自动恢复…'
    })).toBe('识别引擎停滞，正在自动恢复…');
    expect(warningStripText({ droppedTotal: 2, at: 0, reason: 'stalled' }))
      .toBe('识别引擎停滞，正在自动恢复…');
  });

  it('engine_degraded：优先后端 message（可含降档建议）', () => {
    expect(warningStripText({
      droppedTotal: 0, at: 0, reason: 'engine_degraded',
      message: '当前模型在 CPU 上难以实时，建议切换到 base/small'
    })).toBe('当前模型在 CPU 上难以实时，建议切换到 base/small');
    expect(warningStripText({ droppedTotal: 0, at: 0, reason: 'engine_degraded' }))
      .toBe('识别引擎已降级运行（建议切换更小的模型）');
  });

  it('未知 reason：泛化文案 + 可用丢弃数，不抛错、无空条', () => {
    expect(warningStripText({ droppedTotal: 5, at: 0, reason: 'future_reason' }))
      .toContain('累计丢弃 5 句');
    expect(warningStripText({ droppedTotal: 0, at: 0, reason: 'future_reason' }))
      .toContain('识别引擎告警');
    expect(warningStripText(null)).toBe('');
  });
});

describe('OverlayApp 告警细条渲染', () => {
  it('stalled 渲染后端 message 文案', async () => {
    const { container } = await renderOverlay();
    act(() => {
      patchCb?.({
        lastWarning: {
          droppedTotal: 2, at: 1, reason: 'stalled', message: '识别引擎停滞，正在自动恢复…'
        }
      });
    });
    expect(container.querySelectorAll('.warn-strip').length).toBe(1);
    expect(screen.getByText('识别引擎停滞，正在自动恢复…')).not.toBeNull();
  });

  it('engine_degraded 渲染降级说明', async () => {
    await renderOverlay();
    act(() => {
      patchCb?.({
        lastWarning: {
          droppedTotal: 0, at: 1, reason: 'engine_degraded',
          message: '当前模型在 CPU 上难以实时，建议切换到 base/small'
        }
      });
    });
    expect(screen.getByText('当前模型在 CPU 上难以实时，建议切换到 base/small')).not.toBeNull();
  });

  it('未知 reason 渲染泛化文案（含丢弃数）', async () => {
    await renderOverlay();
    act(() => {
      patchCb?.({ lastWarning: { droppedTotal: 9, at: 1, reason: 'future_reason' } });
    });
    expect(screen.getByText('识别引擎告警 · 累计丢弃 9 句')).not.toBeNull();
  });

  it('连续告警刷新同一条不堆叠，约 2s 后自动消失', async () => {
    vi.useFakeTimers();
    const { container } = await renderOverlay();

    act(() => {
      patchCb?.({ lastWarning: { droppedTotal: 3, at: 1, reason: 'queue_full' } });
    });
    expect(container.querySelectorAll('.warn-strip').length).toBe(1);
    expect(screen.getByText('处理过载 · 累计丢弃 3 句（建议切换更小的模型）')).not.toBeNull();

    act(() => {
      patchCb?.({ lastWarning: { droppedTotal: 4, at: 2, reason: 'queue_full' } });
    });
    expect(container.querySelectorAll('.warn-strip').length).toBe(1);
    expect(screen.getByText('处理过载 · 累计丢弃 4 句（建议切换更小的模型）')).not.toBeNull();

    act(() => { vi.advanceTimersByTime(2000); });
    expect(container.querySelectorAll('.warn-strip').length).toBe(0);
  });
});
