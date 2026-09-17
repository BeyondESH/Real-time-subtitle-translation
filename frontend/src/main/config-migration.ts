/**
 * 配置迁移（纯 Node 模块，不 import electron，可独立单测）
 *
 * settings-management spec "配置双源边界与同步"：
 * - electron-store 是用户偏好唯一来源（AppConfig 为规范 schema）
 * - 旧用户 config.yaml 副本中 subtitle/system/shortcuts 段的值一次性导入 store，
 *   原文件不重写；迁移完成后置 legacyMigrated 标记
 */
import * as fs from 'fs';
import { load as loadYaml } from 'js-yaml';

// ---------- 规范 schema（向后兼容旧 store 键路径） ----------

export interface WindowConfig {
  width: number;
  height: number;
  x: number | null;
  y: number | null;
  opacity: number;
  /** 悬浮窗所在显示器 id；null=跟随主显示器（多显示器支持，P3） */
  displayId: number | null;
  /** 按显示器记忆的位置（key=display id 字符串） */
  positions: Record<string, { x: number; y: number }>;
}

export interface SubtitleConfig {
  fontFamily: string;
  fontSize: number;
  fontColor: string;
  strokeColor: string;
  strokeWidth: number;
  displayMode: 'translation_only' | 'original_and_translation';
  /** 外观预设：text=纯文字（默认）、acrylic=毛玻璃胶囊（Win11） */
  preset: 'text' | 'acrylic';
}

export interface ShortcutSet {
  togglePause: string;
  switchLanguage: string;
  switchModel: string;
  toggleLock: string;
}

export interface AppConfig {
  window: WindowConfig;
  subtitle: SubtitleConfig;
  websocket: { host: string; port: number };
  shortcuts: ShortcutSet;
  /** 全局快捷键注册结果（false=被占用，设置页警示） */
  shortcutStatus: Record<keyof ShortcutSet, boolean>;
  translation: { targetLanguages: string[]; activeLanguage: string };
  asr: { model: string };
  audio: { sourceId: string };
  locked: boolean;
  /** 主题：dark（默认）/ light / system */
  theme: 'dark' | 'light' | 'system';
  system: { autoStart: boolean };
  /** 会话静音自动切分（分钟）；null=关闭 */
  sessions: { autoSplitSilenceMin: number | null };
  ui: {
    sidebarCollapsed: boolean;
    mainWindow: { width: number; height: number; x: number | null; y: number | null };
  };
  onboarding: { completed: boolean };
  /** 旧 config.yaml 偏好段迁移完成标记 */
  legacyMigrated: boolean;
}

export const CONFIG_DEFAULTS: AppConfig = {
  window: {
    width: 800, height: 200, x: null, y: null, opacity: 0.9,
    displayId: null, positions: {}
  },
  subtitle: {
    fontFamily: 'Microsoft YaHei',
    fontSize: 24,
    fontColor: '#FFFFFF',
    strokeColor: '#000000',
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
  audio: { sourceId: '' },
  locked: true,
  theme: 'dark',
  system: { autoStart: false },
  sessions: { autoSplitSilenceMin: null },
  ui: {
    sidebarCollapsed: false,
    mainWindow: { width: 1080, height: 720, x: null, y: null }
  },
  onboarding: { completed: false },
  legacyMigrated: false
};

// ---------- 旧 config.yaml → store 一次性迁移 ----------

export interface MigrationEntry {
  /** electron-store 点路径，如 subtitle.fontFamily */
  key: string;
  value: string | number | boolean | null;
}

export interface LegacyMigrationPlan {
  entries: MigrationEntry[];
  /** 被跳过的键（类型不符/无 store 对应项） */
  notes: string[];
}

interface YamlRecord { [k: string]: unknown }

function isRecord(v: unknown): v is YamlRecord {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

/**
 * 解析旧 config.yaml 文本，产出 store 迁移计划。
 * 无 legacy 段（subtitle/system/shortcuts）或解析失败 → null。
 */
export function planLegacyYamlMigration(yamlText: string): LegacyMigrationPlan | null {
  let doc: unknown;
  try {
    doc = loadYaml(yamlText);
  } catch {
    return null;
  }
  if (!isRecord(doc)) return null;

  const entries: MigrationEntry[] = [];
  const notes: string[] = [];

  const subtitle = doc.subtitle;
  const system = doc.system;
  const shortcuts = doc.shortcuts;
  if (subtitle === undefined && system === undefined && shortcuts === undefined) {
    return null; // 新模板已是瘦配置，无需迁移
  }

  if (isRecord(subtitle)) {
    const font = subtitle.font;
    if (isRecord(font)) {
      if (typeof font.family === 'string') entries.push({ key: 'subtitle.fontFamily', value: font.family });
      if (typeof font.size === 'number') entries.push({ key: 'subtitle.fontSize', value: font.size });
      if (typeof font.color === 'string') entries.push({ key: 'subtitle.fontColor', value: font.color });
      if (typeof font.stroke_color === 'string') entries.push({ key: 'subtitle.strokeColor', value: font.stroke_color });
      if (typeof font.stroke_width === 'number') entries.push({ key: 'subtitle.strokeWidth', value: font.stroke_width });
    } else if (font !== undefined) {
      notes.push('subtitle.font 非映射，跳过');
    }
    const win = subtitle.window;
    if (isRecord(win)) {
      if (typeof win.width === 'number') entries.push({ key: 'window.width', value: win.width });
      if (typeof win.height === 'number') entries.push({ key: 'window.height', value: win.height });
      if (typeof win.x === 'number' || win.x === null) entries.push({ key: 'window.x', value: win.x });
      if (typeof win.y === 'number' || win.y === null) entries.push({ key: 'window.y', value: win.y });
      if (typeof win.opacity === 'number') entries.push({ key: 'window.opacity', value: win.opacity });
    }
    if (subtitle.display_mode === 'translation_only' || subtitle.display_mode === 'original_and_translation') {
      entries.push({ key: 'subtitle.displayMode', value: subtitle.display_mode });
    }
  }

  if (isRecord(system)) {
    if (typeof system.auto_start === 'boolean') {
      entries.push({ key: 'system.autoStart', value: system.auto_start });
    }
    if (system.minimize_to_tray !== undefined || system.log_level !== undefined) {
      notes.push('system.minimize_to_tray/log_level 无 store 对应项，忽略');
    }
  }

  if (isRecord(shortcuts)) {
    const map: Array<[string, string]> = [
      ['toggle_pause', 'shortcuts.togglePause'],
      ['switch_language', 'shortcuts.switchLanguage'],
      ['switch_model', 'shortcuts.switchModel'],
      ['toggle_lock', 'shortcuts.toggleLock']
    ];
    for (const [yamlKey, storeKey] of map) {
      const v = shortcuts[yamlKey];
      if (typeof v === 'string' && v.length > 0) entries.push({ key: storeKey, value: v });
    }
  }

  return { entries, notes };
}

export interface LegacyMigrationLogger {
  info(...args: unknown[]): void;
  warn(...args: unknown[]): void;
}

/**
 * 执行一次性迁移（幂等）：isMigrated 为 true 时直接跳过。
 * 只读 config.yaml，绝不重写原文件（spec：原文件不重写）。
 */
export function runLegacyYamlMigration(opts: {
  configYamlPath: string;
  isMigrated(): boolean;
  markMigrated(): void;
  applyEntry(entry: MigrationEntry): void;
  logger: LegacyMigrationLogger;
}): { applied: number } | null {
  if (opts.isMigrated()) return null;

  if (!fs.existsSync(opts.configYamlPath)) {
    opts.markMigrated(); // 新装用户：无旧文件，直接置位
    return { applied: 0 };
  }

  let text: string;
  try {
    text = fs.readFileSync(opts.configYamlPath, 'utf8');
  } catch (err) {
    opts.logger.warn('旧配置不可读，跳过迁移', err);
    return null; // 不置位，下次启动重试
  }

  const plan = planLegacyYamlMigration(text);
  if (!plan) {
    opts.markMigrated();
    return { applied: 0 };
  }

  for (const entry of plan.entries) {
    opts.applyEntry(entry);
  }
  for (const note of plan.notes) opts.logger.info('legacy-yaml:', note);
  opts.markMigrated();
  opts.logger.info(`legacy config.yaml 迁移完成: ${plan.entries.length} 项`);
  return { applied: plan.entries.length };
}
