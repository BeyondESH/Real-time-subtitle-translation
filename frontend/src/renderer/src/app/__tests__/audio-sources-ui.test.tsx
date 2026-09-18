// @vitest-environment jsdom
/**
 * 三处音频源 UI（add-per-process-audio-capture 8.2/8.3/8.4）：
 * 不支持说明、序号渲染、空态、单侧 WS 失败不崩溃、选择派发结构化源、面板轮询。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字，禁止 hex/rgb。
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup, waitFor, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AudioSection } from '../SettingsPage';
import { StatusPillBar } from '../StatusPillBar';
import { OnboardingPage } from '../OnboardingPage';
import { AUDIO_APPS_EMPTY_HINT, AUDIO_APPS_UNSUPPORTED_HINT } from '../../state/audio-sources';

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
      switchModel: 'Ctrl+Shift+M',
      toggleLock: 'Ctrl+Shift+D'
    },
    shortcutStatus: {
      togglePause: true, switchLanguage: true, switchModel: true, toggleLock: true
    },
    translation: { targetLanguages: ['zh', 'en'], activeLanguage: 'zh', model: 'hy-mt2-1.8b-q4km' },
    asr: { model: 'base' },
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
  result: [{ id: 'd1', name: '扬声器', is_loopback: true }]
};

function processesResponse(processes: unknown[]): WsResponseView {
  return { ok: true, result: { supported: true, reason: null, processes } };
}

function audioHandler(devices: WsResponseView, processes: WsResponseView): Handler {
  return async (method) => (method === 'get_audio_sources' ? devices : processes);
}

describe('AudioSection（设置页音频分段）', () => {
  it('supported=false → 显示不支持说明，设备仍可选且无假应用数据', async () => {
    installApi(audioHandler(
      DEVICES_OK,
      { ok: true, result: { supported: false, reason: 'os_too_old', processes: [] } }
    ));
    render(<AudioSection cfg={makeConfig({ kind: 'device', id: '' })} />);
    expect(await screen.findByText(AUDIO_APPS_UNSUPPORTED_HINT)).not.toBeNull();
    expect(screen.getByRole('option', { name: '扬声器' })).not.toBeNull();
    expect(screen.queryByRole('option', { name: /chrome\.exe/ })).toBeNull();
  });

  it('同名多实例渲染序号；选择进程派发结构化源', async () => {
    const dispatch = installApi(audioHandler(
      DEVICES_OK,
      processesResponse([
        { pid: 1, name: 'chrome.exe', active: true, ordinal: 1 },
        { pid: 2, name: 'chrome.exe', active: true, ordinal: 2 }
      ])
    ));
    render(<AudioSection cfg={makeConfig({ kind: 'device', id: '' })} />);
    expect(await screen.findByRole('option', { name: 'chrome.exe (1)' })).not.toBeNull();
    expect(screen.getByRole('option', { name: 'chrome.exe (2)' })).not.toBeNull();

    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: 'process:2:chrome.exe' }
    });
    expect(dispatch).toHaveBeenCalledWith({
      type: 'setAudioSource',
      source: { kind: 'process', pid: 2, name: 'chrome.exe' }
    });
  });

  it('supported=true 但无发声应用 → 非误导性空态文案', async () => {
    installApi(audioHandler(DEVICES_OK, processesResponse([])));
    render(<AudioSection cfg={makeConfig({ kind: 'device', id: '' })} />);
    expect(await screen.findByText(AUDIO_APPS_EMPTY_HINT)).not.toBeNull();
    expect(screen.queryByRole('option', { name: /chrome\.exe/ })).toBeNull();
  });

  it('应用请求失败不崩溃：设备列表仍可用并显示错误+重试', async () => {
    installApi(async (method) => method === 'get_audio_sources'
      ? DEVICES_OK
      : Promise.reject(new Error('boom')));
    render(<AudioSection cfg={makeConfig({ kind: 'device', id: '' })} />);
    expect(await screen.findByText(/应用列表/)).not.toBeNull();
    expect(screen.getByRole('option', { name: '扬声器' })).not.toBeNull();
    expect(screen.getAllByText('重试').length).toBeGreaterThan(0);
  });
});

describe('StatusPillBar（胶囊面板）', () => {
  it('面板渲染分组与序号，选择进程派发结构化源并关闭', async () => {
    const dispatch = installApi(audioHandler(
      DEVICES_OK,
      processesResponse([{ pid: 1234, name: 'chrome.exe', active: true, ordinal: 1 }])
    ));
    render(<StatusPillBar state={makeState({ audioSource: '' })} />);
    fireEvent.click(screen.getByTitle('音频源'));

    const item = await screen.findByRole('button', { name: 'chrome.exe (1)' });
    fireEvent.click(item);
    expect(dispatch).toHaveBeenCalledWith({
      type: 'setAudioSource',
      source: { kind: 'process', pid: 1234, name: 'chrome.exe' }
    });
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'chrome.exe (1)' })).toBeNull()
    );
  });

  it('supported=false → 面板不显示应用区并附说明', async () => {
    installApi(audioHandler(
      DEVICES_OK,
      { ok: true, result: { supported: false, reason: 'os_too_old', processes: [] } }
    ));
    render(<StatusPillBar state={makeState({ audioSource: '' })} />);
    fireEvent.click(screen.getByTitle('音频源'));
    expect(await screen.findByText(AUDIO_APPS_UNSUPPORTED_HINT)).not.toBeNull();
    expect(screen.getByText('设备')).not.toBeNull();
  });

  it('打开期间每 3s 轮询，关闭后停止', async () => {
    vi.useFakeTimers();
    let calls = 0;
    installApi(async (method) => {
      calls += 1;
      return method === 'get_audio_sources' ? DEVICES_OK : processesResponse([]);
    });
    render(<StatusPillBar state={makeState({ audioSource: '' })} />);
    fireEvent.click(screen.getByTitle('音频源'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(calls).toBe(2); // 打开即并行拉取两接口

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(calls).toBe(4);

    fireEvent.click(screen.getByLabelText('关闭面板'));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });
    expect(calls).toBe(4);
  });
});

describe('OnboardingPage（引导音频步）', () => {
  it('默认选中整个系统；展示应用；默认下一步不派发', async () => {
    const dispatch = installApi(audioHandler(
      DEVICES_OK,
      processesResponse([{ pid: 5, name: 'chrome.exe', active: true, ordinal: null }])
    ));
    render(
      <MemoryRouter>
        <OnboardingPage />
      </MemoryRouter>
    );
    fireEvent.click(screen.getByText('开始设置'));

    const select = await screen.findByRole('combobox');
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe('device:'));
    expect(screen.getByRole('option', { name: 'chrome.exe' })).not.toBeNull();

    fireEvent.click(screen.getByText('下一步'));
    expect(dispatch).not.toHaveBeenCalled();
  });

  it('选择应用后下一步派发结构化进程源', async () => {
    const dispatch = installApi(audioHandler(
      DEVICES_OK,
      processesResponse([{ pid: 5, name: 'chrome.exe', active: true, ordinal: null }])
    ));
    render(
      <MemoryRouter>
        <OnboardingPage />
      </MemoryRouter>
    );
    fireEvent.click(screen.getByText('开始设置'));
    const select = await screen.findByRole('combobox');
    await waitFor(() => expect((select as HTMLSelectElement).value).toBe('device:'));

    fireEvent.change(select, { target: { value: 'process:5:chrome.exe' } });
    fireEvent.click(screen.getByText('下一步'));
    expect(dispatch).toHaveBeenCalledWith({
      type: 'setAudioSource',
      source: { kind: 'process', pid: 5, name: 'chrome.exe' }
    });
  });
});
