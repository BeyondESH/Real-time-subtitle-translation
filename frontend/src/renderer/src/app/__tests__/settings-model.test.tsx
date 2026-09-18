// @vitest-environment jsdom
/**
 * 推理设备设置行（settings-management spec「推理设备设置与状态显示」）：
 * 默认 auto 渲染、状态行精确文案、静默降级时控件值保持用户选择。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字（white/black），禁止 hex/rgb。
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { ModelSection, deviceStatusText, deviceModelAdvice } from '../SettingsPage';

afterEach(cleanup);

const TRANSLATION_RESULT = {
  translation: {
    model: 'hy-mt2-1.8b-q4km',
    available_models: [
      {
        id: 'hy-mt2-1.8b-q4km', display_name: 'Hy-MT2 1.8B（推荐）',
        size_bytes: 1133080448, downloaded: true, current: true
      },
      {
        id: 'qwen3-1.7b-q4km', display_name: 'Qwen3 1.7B（通用/多语兜底）',
        size_bytes: 1282439584, downloaded: false, current: false
      }
    ]
  }
};

function installAppAPI(result: unknown = TRANSLATION_RESULT): void {
  window.appAPI = {
    wsRequest: vi.fn(async () => ({ ok: true, result })),
    dispatch: vi.fn(async () => undefined)
  } as unknown as AppAPI;
}

beforeEach(() => { installAppAPI(); });

type DeviceView = AppStateView['device'];

function engine(
  resolved: 'cuda' | 'cpu' | null,
  reason: 'auto' | 'user' | 'no_cuda' | 'load_failed' | 'runtime_failed'
) {
  return { resolved, reason };
}

function makeState(device: DeviceView): AppStateView {
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
    device,
    activeSessionId: 'sess-1'
  };
}

function makeConfig(device: 'auto' | 'cpu' | 'cuda'): AppConfigView {
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
    inference: { device },
    audio: { source: { kind: 'device', id: '' } },
    locked: true,
    theme: 'dark',
    system: { autoStart: false },
    sessions: { autoSplitSilenceMin: null },
    ui: { sidebarCollapsed: false, mainWindow: { width: 1080, height: 720, x: null, y: null } },
    onboarding: { completed: true }
  };
}

describe('deviceStatusText（精确文案映射）', () => {
  it('resolved 缺失/null → 正在检测推理设备…', () => {
    expect(deviceStatusText(null)).toBe('正在检测推理设备…');
    expect(deviceStatusText(undefined)).toBe('正在检测推理设备…');
    expect(deviceStatusText({ asr: engine(null, 'auto'), translation: engine(null, 'auto') }))
      .toBe('正在检测推理设备…');
  });

  it('cuda + auto/user', () => {
    expect(deviceStatusText({ asr: engine('cuda', 'auto'), translation: engine(null, 'auto') }))
      .toBe('GPU（CUDA 自动检测）');
    expect(deviceStatusText({ asr: engine('cuda', 'user'), translation: engine(null, 'auto') }))
      .toBe('GPU（用户指定）');
  });

  it('cpu + no_cuda/load_failed/user', () => {
    expect(deviceStatusText({ asr: engine('cpu', 'no_cuda'), translation: engine(null, 'auto') }))
      .toBe('CPU（未检测到兼容的 CUDA 环境）');
    expect(deviceStatusText({ asr: engine('cpu', 'load_failed'), translation: engine(null, 'auto') }))
      .toBe('CPU（GPU 加载失败，已自动降级）');
    expect(deviceStatusText({ asr: engine('cpu', 'user'), translation: engine(null, 'auto') }))
      .toBe('CPU（用户指定）');
  });

  it('cpu + runtime_failed → GPU 运行时不可用降级文案', () => {
    expect(deviceStatusText({ asr: engine('cpu', 'runtime_failed'), translation: engine(null, 'auto') }))
      .toBe('CPU（GPU 运行时不可用，已自动降级）');
  });
});

describe('deviceModelAdvice（重档模型 × CPU 降档建议，design D6）', () => {
  const cpu = { asr: engine('cpu', 'runtime_failed'), translation: engine(null, 'auto') };

  it('CPU + medium/large-v3 → 建议降档（含各降级原因）', () => {
    expect(deviceModelAdvice(cpu, 'medium')).toBe('当前模型在 CPU 上难以实时，建议切换到更小模型档位');
    expect(deviceModelAdvice(cpu, 'large-v3')).toBe('当前模型在 CPU 上难以实时，建议切换到更小模型档位');
    expect(deviceModelAdvice(
      { asr: engine('cpu', 'no_cuda'), translation: engine(null, 'auto') }, 'medium'
    )).toBe('当前模型在 CPU 上难以实时，建议切换到更小模型档位');
  });

  it('非重档模型 / GPU / 检测中 → 无建议', () => {
    expect(deviceModelAdvice(cpu, 'base')).toBeNull();
    expect(deviceModelAdvice(cpu, 'small')).toBeNull();
    expect(deviceModelAdvice(cpu, undefined)).toBeNull();
    expect(deviceModelAdvice(
      { asr: engine('cuda', 'auto'), translation: engine(null, 'auto') }, 'medium'
    )).toBeNull();
    expect(deviceModelAdvice(null, 'medium')).toBeNull();
  });
});

describe('ModelSection 推理设备', () => {
  it('默认 auto：下拉值 auto，状态行显示检测结果', () => {
    render(<ModelSection state={makeState(null)} cfg={makeConfig('auto')} />);
    const select = screen.getAllByRole('combobox')[2] as HTMLSelectElement;
    expect(select.value).toBe('auto');
    expect(screen.getByText('当前使用：正在检测推理设备…')).not.toBeNull();
  });

  it('降级如实呈报：控件保持用户选择 GPU，状态行显示降级原因', () => {
    render(
      <ModelSection
        state={makeState({ asr: engine('cpu', 'load_failed'), translation: engine('cpu', 'no_cuda') })}
        cfg={makeConfig('cuda')}
      />
    );
    const select = screen.getAllByRole('combobox')[2] as HTMLSelectElement;
    expect(select.value).toBe('cuda');
    expect(screen.getByText('当前使用：CPU（GPU 加载失败，已自动降级）')).not.toBeNull();
  });

  it('GPU 生效：显示 CUDA 自动检测文案', () => {
    render(
      <ModelSection
        state={makeState({ asr: engine('cuda', 'auto'), translation: engine('cpu', 'no_cuda') })}
        cfg={makeConfig('auto')}
      />
    );
    expect(screen.getByText('当前使用：GPU（CUDA 自动检测）')).not.toBeNull();
  });

  it('运行期不可用降级：状态行如实呈报，重档模型附降档建议且不自动改选', () => {
    const st = makeState({ asr: engine('cpu', 'runtime_failed'), translation: engine('cpu', 'no_cuda') });
    st.model = 'medium';
    render(<ModelSection state={st} cfg={makeConfig('cuda')} />);
    const modelSelect = screen.getAllByRole('combobox')[0] as HTMLSelectElement;
    expect(modelSelect.value).toBe('medium'); // MUST NOT 自动更改用户模型选择
    expect(screen.getByText('当前使用：CPU（GPU 运行时不可用，已自动降级）')).not.toBeNull();
    expect(screen.getByText('当前模型在 CPU 上难以实时，建议切换到更小模型档位')).not.toBeNull();
  });

  it('轻档模型 CPU 时不显示降档建议', () => {
    render(
      <ModelSection
        state={makeState({ asr: engine('cpu', 'runtime_failed'), translation: engine('cpu', 'no_cuda') })}
        cfg={makeConfig('cuda')}
      />
    );
    expect(screen.queryByText('当前模型在 CPU 上难以实时，建议切换到更小模型档位')).toBeNull();
  });
});

describe('ModelSection 翻译模型（replace-translation-engine-with-llamacpp 5.2）', () => {
  it('渲染注册表列表与服务状态（后端实际模型 = store 偏好）', async () => {
    render(<ModelSection state={makeState(null)} cfg={makeConfig('auto')} />);
    const select = (await screen.findAllByRole('combobox'))[1] as HTMLSelectElement;
    expect(select.value).toBe('hy-mt2-1.8b-q4km');
    expect(await screen.findByText('当前使用：Hy-MT2 1.8B（推荐）')).not.toBeNull();
    expect(screen.getByText(/Qwen3 1.7B/)).not.toBeNull();
  });

  it('后端实际模型与 store 不一致 → 加载/对齐中（不冒充偏好值）', async () => {
    installAppAPI({ translation: { model: 'qwen3-1.7b-q4km', available_models: [] } });
    render(<ModelSection state={makeState(null)} cfg={makeConfig('auto')} />);
    expect(await screen.findByText('正在加载/对齐中…')).not.toBeNull();
  });

  it('选择即派发 setLlm Intent（改动即落盘）', async () => {
    render(<ModelSection state={makeState(null)} cfg={makeConfig('auto')} />);
    const select = (await screen.findAllByRole('combobox'))[1] as HTMLSelectElement;
    fireEvent.change(select, { target: { value: 'qwen3-1.7b-q4km' } });
    expect(window.appAPI.dispatch).toHaveBeenCalledWith({
      type: 'setLlm', modelId: 'qwen3-1.7b-q4km'
    });
  });

  it('后端未连接（wsRequest 拒绝）→ 错误态文案；当前选择仍在列', async () => {
    window.appAPI = {
      wsRequest: vi.fn(async () => { throw new Error('offline'); }),
      dispatch: vi.fn(async () => undefined)
    } as unknown as AppAPI;
    render(<ModelSection state={makeState(null)} cfg={makeConfig('auto')} />);
    expect(await screen.findByText('翻译模型信息拉取失败（后端未连接）')).not.toBeNull();
    const select = screen.getAllByRole('combobox')[1] as HTMLSelectElement;
    expect(select.value).toBe('hy-mt2-1.8b-q4km');
  });
});
