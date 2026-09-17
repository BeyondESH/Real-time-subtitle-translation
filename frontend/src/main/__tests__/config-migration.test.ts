/**
 * 旧 config.yaml → store 迁移单测（settings-management spec 场景）
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { planLegacyYamlMigration, runLegacyYamlMigration, type MigrationEntry } from '../config-migration';

const LEGACY_YAML = `
audio:
  sample_rate: 16000
subtitle:
  font:
    family: "SimHei"
    size: 30
    color: "#FFEE00"
    stroke_color: "#112233"
    stroke_width: 3
  window:
    width: 900
    height: 220
    x: null
    y: 100
    opacity: 0.8
  display_mode: translation_only
shortcuts:
  toggle_pause: "Ctrl+Alt+Space"
  switch_language: "Ctrl+Alt+L"
system:
  auto_start: true
  minimize_to_tray: true
  log_level: DEBUG
`;

const SLIM_YAML = `
audio:
  sample_rate: 16000
pipeline:
  tick_ms: 250
`;

describe('planLegacyYamlMigration', () => {
  it('legacy 段全部映射为 store 点路径 entries', () => {
    const plan = planLegacyYamlMigration(LEGACY_YAML);
    expect(plan).not.toBeNull();
    const map = new Map(plan!.entries.map((e) => [e.key, e.value]));
    expect(map.get('subtitle.fontFamily')).toBe('SimHei');
    expect(map.get('subtitle.fontSize')).toBe(30);
    expect(map.get('subtitle.fontColor')).toBe('#FFEE00');
    expect(map.get('subtitle.strokeColor')).toBe('#112233');
    expect(map.get('subtitle.strokeWidth')).toBe(3);
    expect(map.get('window.width')).toBe(900);
    expect(map.get('window.x')).toBeNull();
    expect(map.get('window.y')).toBe(100);
    expect(map.get('window.opacity')).toBe(0.8);
    expect(map.get('subtitle.displayMode')).toBe('translation_only');
    expect(map.get('shortcuts.togglePause')).toBe('Ctrl+Alt+Space');
    expect(map.get('shortcuts.switchLanguage')).toBe('Ctrl+Alt+L');
    expect(map.get('system.autoStart')).toBe(true);
  });

  it('无 legacy 段（瘦模板）→ null', () => {
    expect(planLegacyYamlMigration(SLIM_YAML)).toBeNull();
  });

  it('非法 YAML → null 而非抛错', () => {
    expect(planLegacyYamlMigration('subtitle: [oops')).toBeNull();
    expect(planLegacyYamlMigration('')).toBeNull();
  });

  it('类型不符的值被跳过，minimize_to_tray/log_level 记入 notes', () => {
    const plan = planLegacyYamlMigration(`
subtitle:
  font:
    size: "big"
system:
  auto_start: "yes"
  minimize_to_tray: true
  log_level: INFO
`);
    expect(plan).not.toBeNull();
    expect(plan!.entries.length).toBe(0);
    expect(plan!.notes.some((n) => n.includes('minimize_to_tray'))).toBe(true);
  });
});

describe('runLegacyYamlMigration（幂等 + 不重写原文件）', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cfg-mig-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('首次执行应用 entries 并置位标记；二次执行跳过', () => {
    const yamlPath = path.join(tmpDir, 'config.yaml');
    fs.writeFileSync(yamlPath, LEGACY_YAML, 'utf8');
    const before = fs.readFileSync(yamlPath, 'utf8');

    let migrated = false;
    const applied: MigrationEntry[] = [];
    const logger = { info: () => undefined, warn: () => undefined };

    const first = runLegacyYamlMigration({
      configYamlPath: yamlPath,
      isMigrated: () => migrated,
      markMigrated: () => { migrated = true; },
      applyEntry: (e) => applied.push(e),
      logger
    });
    expect(first).not.toBeNull();
    expect(first!.applied).toBeGreaterThan(0);
    expect(applied.some((e) => e.key === 'system.autoStart' && e.value === true)).toBe(true);
    // 原文件字节级不变（spec：绝不重写）
    expect(fs.readFileSync(yamlPath, 'utf8')).toBe(before);

    const second = runLegacyYamlMigration({
      configYamlPath: yamlPath,
      isMigrated: () => migrated,
      markMigrated: () => { migrated = true; },
      applyEntry: (e) => applied.push(e),
      logger
    });
    expect(second).toBeNull();
  });

  it('文件不存在（新装用户）→ 直接置位标记，applied=0', () => {
    let migrated = false;
    const r = runLegacyYamlMigration({
      configYamlPath: path.join(tmpDir, 'missing.yaml'),
      isMigrated: () => migrated,
      markMigrated: () => { migrated = true; },
      applyEntry: () => undefined,
      logger: { info: () => undefined, warn: () => undefined }
    });
    expect(r).toEqual({ applied: 0 });
    expect(migrated).toBe(true);
  });

  it('文件不可读 → 返回 null 且不置位（下次启动重试）', () => {
    let migrated = false;
    const r = runLegacyYamlMigration({
      configYamlPath: path.join(tmpDir, '..') + path.sep + '__definitely_missing__' + path.sep + 'x.yaml',
      isMigrated: () => migrated,
      markMigrated: () => { migrated = true; },
      applyEntry: () => undefined,
      logger: { info: () => undefined, warn: () => undefined }
    });
    // 目录级 existsSync=false → 走"文件不存在"分支置位
    expect(r).toEqual({ applied: 0 });
    expect(migrated).toBe(true);
  });
});
