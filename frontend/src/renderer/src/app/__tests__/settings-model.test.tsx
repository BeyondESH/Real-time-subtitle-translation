// @vitest-environment jsdom
/**
 * 推理设备设置行（settings-management spec「推理设备设置与状态显示」）：
 * 默认 auto 渲染、状态行精确文案、静默降级时控件值保持用户选择。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字（white/black），禁止 hex/rgb。
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { ModelSection, deviceStatusText } from '../SettingsPage';

afterEach(cleanup);

type DeviceView = AppStateView['device'];

function engine(resolved: 'cuda' | 'cpu' | null, reason: 'auto' | 'user' | 'no_cuda' | 'load_failed') {
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
    translation: { targetLanguages: ['zh', 'en'], activeLanguage: 'zh' },
    asr: { model: 'base' },
    inference: { device },
    audio: { sourceId: '' },
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
});

describe('ModelSection 推理设备', () => {
  it('默认 auto：下拉值 auto，状态行显示检测结果', () => {
    render(<ModelSection state={makeState(null)} cfg={makeConfig('auto')} />);
    const select = screen.getAllByRole('combobox')[1] as HTMLSelectElement;
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
    const select = screen.getAllByRole('combobox')[1] as HTMLSelectElement;
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
});
