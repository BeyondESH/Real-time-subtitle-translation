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
import {
  DEFAULT_AUDIO_SOURCE, type AudioSourcePref, type AudioSourceTarget
} from '../shared/ipc-types';

export type { AudioSourcePref, AudioSourceTarget } from '../shared/ipc-types';

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

/** 推理设备偏好（统一管 ASR 与翻译；与后端控制协议同词汇） */
export type InferenceDevice = 'auto' | 'cpu' | 'cuda';

/** 默认翻译模型（与后端注册表默认一致；store 偏好键 translation.model） */
export const DEFAULT_TRANSLATION_MODEL = 'hy-mt2-1.8b-q4km';

export interface AppConfig {
  window: WindowConfig;
  subtitle: SubtitleConfig;
  websocket: { host: string; port: number };
  shortcuts: ShortcutSet;
  /** 全局快捷键注册结果（false=被占用，设置页警示） */
  shortcutStatus: Record<keyof ShortcutSet, boolean>;
  translation: { targetLanguages: string[]; activeLanguage: string; model: string };
  asr: { model: string };
  /** 推理设备偏好；auto=后端自动探测（显式 cpu/cuda 经 SUBTITLE_DEVICE 注入） */
  inference: { device: InferenceDevice };
  /** 音频源偏好（结构化：设备源或按进程源） */
  audio: { source: AudioSourcePref };
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
  translation: {
    targetLanguages: ['zh', 'en'],
    activeLanguage: 'zh',
    model: DEFAULT_TRANSLATION_MODEL
  },
  asr: { model: 'base' },
  inference: { device: 'auto' },
  audio: { source: { ...DEFAULT_AUDIO_SOURCE } },
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

// ---------- 音频源（结构化，settings-management spec D10） ----------

function isAudioSourcePref(v: unknown): v is AudioSourcePref {
  if (v === null || typeof v !== 'object') return false;
  const rec = v as Record<string, unknown>;
  if (rec.kind === 'device') return typeof rec.id === 'string';
  if (rec.kind === 'process') {
    return typeof rec.name === 'string'
      && (rec.lastPid === null || typeof rec.lastPid === 'number');
  }
  return false;
}

/**
 * 音频源规范化（幂等）：`audio.source` 合法时原样返回；缺失/形状非法时
 * 按旧裸字符串 `audio.sourceId` 解释为设备源（`''` = 默认设备）。
 * 旧键 `sourceId` 由调用方保留不改写，规范化后不再读取。
 */
export function resolveAudioSection(raw: unknown): { source: AudioSourcePref; changed: boolean } {
  const rec = (raw !== null && typeof raw === 'object' ? raw : {}) as Record<string, unknown>;
  if (isAudioSourcePref(rec.source)) {
    return { source: rec.source, changed: false };
  }
  const legacyId = typeof rec.sourceId === 'string' ? rec.sourceId : '';
  return { source: { kind: 'device', id: legacyId }, changed: true };
}

/** 音频源稳定键（对齐会话级幂等/同值比较）：device:<id> / process:<name> */
export function audioSourceKey(source: AudioSourcePref): string {
  return source.kind === 'device' ? `device:${source.id}` : `process:${source.name}`;
}

/** 同源比较：仅比较形态与标识（进程 PID 变化不视为换源，绑定一律以列表 PID 为准） */
export function sameAudioSource(a: AudioSourcePref, b: AudioSourcePref): boolean {
  return audioSourceKey(a) === audioSourceKey(b);
}

/** 展示/会话历史标签：设备源=id（`''` = 整个系统），进程源=进程名 */
export function audioSourceLabel(source: AudioSourcePref): string {
  return source.kind === 'device' ? source.id : source.name;
}

/** store 偏好 → 线上目标；进程源缺 PID（理论不可达）时返回 null（不可下发） */
export function audioSourceToTarget(pref: AudioSourcePref): AudioSourceTarget | null {
  if (pref.kind === 'device') return { kind: 'device', id: pref.id };
  return pref.lastPid === null ? null : { kind: 'process', pid: pref.lastPid, name: pref.name };
}

/** 线上目标 → store 偏好（进程 pid → lastPid 持久化） */
export function audioSourceFromTarget(target: AudioSourceTarget): AudioSourcePref {
  return target.kind === 'device'
    ? { kind: 'device', id: target.id }
    : { kind: 'process', name: target.name, lastPid: target.pid };
}

/** 线上目标同值比较（含 PID：同名不同实例视为换源） */
export function sameAudioTarget(a: AudioSourceTarget, b: AudioSourceTarget): boolean {
  if (a.kind === 'device' || b.kind === 'device') {
    return a.kind === 'device' && b.kind === 'device' && a.id === b.id;
  }
  return a.pid === b.pid && a.name === b.name;
}

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
