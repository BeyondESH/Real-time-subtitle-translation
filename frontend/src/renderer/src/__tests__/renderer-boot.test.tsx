// @vitest-environment jsdom
/**
 * 渲染层启动冒烟（补齐"主进程冒烟看不到渲染层白屏"的覆盖缺口）：
 * 以 window.appAPI 结构桩驱动真实 App 启动，验证引导页/主窗口两条 boot 路径可渲染。
 *
 * 注：桩数据颜色使用 CSS 关键字（white/black）——本文件在 color-scan 范围内，
 * 禁止 hex/rgb 字面量。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import App from '../App';

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
  activeSessionId: 'sess-1'
};

function makeConfig(over: Partial<AppConfigView['onboarding']> = {}): AppConfigView {
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
    inference: { device: 'auto' },
    audio: { sourceId: '' },
    locked: true,
    theme: 'dark',
    system: { autoStart: false },
    sessions: { autoSplitSilenceMin: null },
    ui: { sidebarCollapsed: false, mainWindow: { width: 1080, height: 720, x: null, y: null } },
    onboarding: { completed: false, ...over }
  };
}

const SESSIONS: SessionRowView[] = [{
  id: 'sess-1',
  title: '生肉直播',
  started_at: Date.now(),
  ended_at: null,
  audio_source: '',
  utterance_count: 0
}];

const unsub = (): (() => void) => () => undefined;

function installApiStub(config: AppConfigView): void {
  window.appAPI = {
    getState: async () => STATE,
    onStatePatch: unsub,
    getConfig: async () => config,
    onConfigChanged: unsub,
    onSubtitle: unsub,
    onToast: unsub,
    onNavigate: unsub,
    onHistoryChanged: unsub,
    onUpdate: unsub,
    dispatch: async () => undefined,
    wsRequest: async () => ({ ok: true, result: [] }),
    getEnv: async () => ({
      platform: 'win32', supportsWco: true, supportsAcrylic: true, version: '2.0.0'
    }),
    setConfig: async () => true,
    openPath: async () => true,
    checkUpdate: async () => ({ phase: 'not-available' }),
    quitAndInstall: async () => undefined,
    getDisplays: async () => [],
    listSessions: async () => SESSIONS,
    getSession: async () => null,
    searchHistory: async () => [],
    renameSession: async () => true,
    deleteSession: async () => ({ deleted: true, wasActive: false }),
    newSession: async () => 'sess-2',
    historyStats: async () => ({ sizeBytes: 4096, sessionCount: 1, utteranceCount: 0 }),
    clearHistory: async () => ({ sizeBytes: 0, sessionCount: 0, utteranceCount: 0 }),
    exportSession: async () => ({ ok: true, filePath: 'x.srt' })
  };
}

beforeEach(() => {
  // jsdom 的 location.hash 在测试间残留：上个用例停在 #/onboarding 会让
  // 下个用例的 HashRouter 直接命中该路由，Navigate 初始重定向失效
  window.location.hash = '#/';
  document.documentElement.removeAttribute('data-theme');
});

afterEach(cleanup);

describe('渲染层启动路径', () => {
  it('首启（onboarding 未完成）→ 引导欢迎页', async () => {
    installApiStub(makeConfig({ completed: false }));
    render(<App />);
    expect(await screen.findByText('欢迎使用实时字幕翻译')).not.toBeNull();
    expect(screen.getByText('开始设置')).not.toBeNull();
  });

  it('二启（onboarding 完成）→ 主窗口直播页 + 侧栏会话列表', async () => {
    installApiStub(makeConfig({ completed: true }));
    render(<App />);
    expect(await screen.findByText('直播字幕')).not.toBeNull();
    expect(await screen.findByText('生肉直播')).not.toBeNull(); // 侧栏真实会话数据
    expect(await screen.findByText('暂无字幕')).not.toBeNull();  // 直播空态
    expect(screen.getByText('运行中')).not.toBeNull();           // 状态胶囊条连接态
  });

  it('启动即应用配置主题（dark → data-theme）', async () => {
    installApiStub(makeConfig({ completed: true }));
    render(<App />);
    await screen.findByText('直播字幕');
    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});
