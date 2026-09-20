// @vitest-environment jsdom
/**
 * 三处音频源 UI（设备-only 回退后）：
 * 设备列表拉取、失败错误+重试、选择派发结构化设备源、引导页默认不派发。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字，禁止 hex/rgb。
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AudioSection } from '../SettingsPage';
import { StatusPillBar } from '../StatusPillBar';
import { OnboardingPage } from '../OnboardingPage';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

type Handler = (method: string) => Promise<WsResponseView>;

function installApi(handler: Handler): ReturnType<typeof vi.fn> {
  const dispatch = vi.fn();
  window.appAPI = {
    wsRequest: (method: string) => handler(method),
    dispatch,
    getState: async () => makeState(),
    onStatePatch: () => () => undefined,
    setConfig: async () => true
  } as unknown as AppAPI;
  return dispatch;
}

function makeState(over: Partial<AppStateView> = {}): AppStateView {
  return {
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
    activeSessionId: 'sess-1',
    ...over
  };
}

function makeConfig(source: AudioSourcePrefView): AppConfigView {
  return {
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
    audio: { source },
    locked: true,
    theme: 'dark',
    system: { autoStart: false },
    sessions: { autoSplitSilenceMin: null },
    ui: { sidebarCollapsed: false, mainWindow: { width: 1080, height: 720, x: null, y: null } },
    onboarding: { completed: true }
  };
}

const DEVICES_OK: WsResponseView = {
  ok: true,
  result: [
    { id: 'd1', name: '扬声器', is_loopback: true },
    { id: 'd2', name: '显示器', is_loopback: true }
  ]
};

function devicesHandler(resp: WsResponseView): Handler {
  return async () => resp;
}

describe('AudioSection（设置页音频分段）', () => {
  it('拉取设备列表：默认回环设备置顶 + 设备名渲染 + 数量提示', async () => {
    installApi(devicesHandler(DEVICES_OK));
    render(<AudioSection cfg={makeConfig({ kind: 'device', id: '' })} />);
    expect(await screen.findByRole('option', { name: '扬声器' })).not.toBeNull();
    expect(screen.getByRole('option', { name: '默认回环设备（推荐）' })).not.toBeNull();
    expect(await screen.findByText(/发现 2 个回环设备/)).not.toBeNull();
  });

  it('设备请求失败 → 显式错误 + 重试入口（不假数据）', async () => {
    installApi(devicesHandler({ ok: false, error: 'not_connected', message: 'x' }));
    render(<AudioSection cfg={makeConfig({ kind: 'device', id: '' })} />);
    expect(await screen.findByText(/后端未连接/)).not.toBeNull();
    expect(screen.getAllByText('重试').length).toBeGreaterThan(0);
  });

  it('选择设备 → 派发结构化设备源', async () => {
    const dispatch = installApi(devicesHandler(DEVICES_OK));
    render(<AudioSection cfg={makeConfig({ kind: 'device', id: '' })} />);
    const select = await screen.findByRole('combobox');
    await waitFor(() => expect(screen.getByRole('option', { name: '扬声器' })).not.toBeNull());
    fireEvent.change(select, { target: { value: 'device:d1' } });
    expect(dispatch).toHaveBeenCalledWith({
      type: 'setAudioSource', source: { kind: 'device', id: 'd1' }
    });
  });
});

describe('StatusPillBar（胶囊面板）', () => {
  it('面板渲染默认回环设备与设备列表，选择后派发并关闭', async () => {
    const dispatch = installApi(devicesHandler(DEVICES_OK));
    render(<StatusPillBar state={makeState({ audioSource: '' })} />);
    fireEvent.click(screen.getByTitle('音频源'));

    const item = await screen.findByRole('button', { name: '扬声器' });
    fireEvent.click(item);
    expect(dispatch).toHaveBeenCalledWith({
      type: 'setAudioSource', source: { kind: 'device', id: 'd1' }
    });
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '扬声器' })).toBeNull()
    );
  });

  it('设备请求失败 → 面板错误行 + 重试', async () => {
    installApi(devicesHandler({ ok: false, error: 'not_connected', message: 'x' }));
    render(<StatusPillBar state={makeState({ audioSource: '' })} />);
    fireEvent.click(screen.getByTitle('音频源'));
    expect(await screen.findByText(/设备：后端未连接/)).not.toBeNull();
    expect(screen.getAllByText('重试').length).toBeGreaterThan(0);
  });

  it('打开面板拉取一次（无轮询）', async () => {
    vi.useFakeTimers();
    let calls = 0;
    installApi(async () => {
      calls += 1;
      return DEVICES_OK;
    });
    render(<StatusPillBar state={makeState({ audioSource: '' })} />);
    fireEvent.click(screen.getByTitle('音频源'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(calls).toBe(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });
    expect(calls).toBe(1);
  });
});

describe('OnboardingPage（引导音频步）', () => {
  it('默认选中默认回环设备；展示设备；下一步不派发', async () => {
    const dispatch = installApi(devicesHandler(DEVICES_OK));
    render(
      <MemoryRouter>
        <OnboardingPage />
      </MemoryRouter>
    );
    fireEvent.click(screen.getByText('开始设置'));

    const select = await screen.findByRole('combobox');
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe('device:'));
    expect(screen.getByRole('option', { name: '扬声器' })).not.toBeNull();

    fireEvent.click(screen.getByText('下一步'));
    expect(dispatch).not.toHaveBeenCalled();
  });

  it('选择设备后下一步派发结构化设备源', async () => {
    const dispatch = installApi(devicesHandler(DEVICES_OK));
    render(
      <MemoryRouter>
        <OnboardingPage />
      </MemoryRouter>
    );
    fireEvent.click(screen.getByText('开始设置'));
    const select = await screen.findByRole('combobox');
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe('device:'));

    fireEvent.change(select, { target: { value: 'device:d1' } });
    fireEvent.click(screen.getByText('下一步'));
    expect(dispatch).toHaveBeenCalledWith({
      type: 'setAudioSource', source: { kind: 'device', id: 'd1' }
    });
  });
});
