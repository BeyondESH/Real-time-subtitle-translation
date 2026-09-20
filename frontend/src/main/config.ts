/**
 * ConfigStore — electron-store 包装（用户偏好唯一来源）
 *
 * settings-management spec：
 * - electron-store 是唯一真相；config.yaml 仅承载后端管线参数
 * - schema 版本化，经 migrations 演进
 * - 旧 config.yaml 偏好段一次性迁移（config-migration.runLegacyYamlMigration）
 */
import Store from 'electron-store';
import type { AppConfig, MigrationEntry } from './config-migration';
import {
  CONFIG_DEFAULTS, resolveAsrSection, resolveAudioSection, runLegacyYamlMigration
} from './config-migration';
import type { BackendLogger } from './backend-manager';

export type { AppConfig } from './config-migration';
export { CONFIG_DEFAULTS } from './config-migration';

/** 点路径写入（electron-store 运行时支持，类型层收窄到 string/number/boolean/null） */
type SetByPath = (key: string, value: string | number | boolean | null) => void;

export class ConfigStore {
  readonly store: Store<AppConfig>;

  constructor() {
    this.store = new Store<AppConfig>({
      defaults: CONFIG_DEFAULTS,
      // electron-store 版本随 app.getVersion()；migrations 以 semver 键演进。
      // '0.0.1'：兼容 1.x 旧 store（同形结构，新增键由 defaults 填齐，无需值变换）。
      migrations: {
        '0.0.1': () => undefined
      }
    });
    this.normalizeWindowSection();
    this.normalizeAudioSection();
    this.normalizeTranslationSection();
    this.normalizeAsrSection();
    this.pruneLegacyShortcutKeys();
  }

  /**
   * 旧用户 store 的 window 段缺少后加键（displayId/positions）——
   * electron-store defaults 不做嵌套合并，此处在启动时一次性补齐。
   */
  private normalizeWindowSection(): void {
    const w = this.store.get('window') as Partial<AppConfig['window']> | undefined;
    if (w && (w.displayId === undefined || w.positions === undefined)) {
      this.store.set('window', {
        ...CONFIG_DEFAULTS.window,
        ...w,
        displayId: w.displayId ?? null,
        positions: w.positions ?? {}
      });
    }
  }

  /**
   * 旧用户 store 的 audio 段为裸字符串 `sourceId`——升级首启规范为结构化
   * `audio.source`（幂等）。点路径写入保留旧键 `sourceId` 不删，此后不再读取
   * （settings-management spec "音频源偏好迁移"）。
   */
  private normalizeAudioSection(): void {
    const raw = this.store.get('audio') as unknown;
    const normalized = resolveAudioSection(raw);
    if (normalized.changed) {
      (this.store.set as unknown as (key: string, value: unknown) => void)(
        'audio.source', normalized.source
      );
    }
  }

  /**
   * 旧用户 store 的 translation 段缺少后加键 `model`——electron-store defaults
   * 不做嵌套合并，启动时一次性补齐（默认翻译模型；幂等）。
   */
  private normalizeTranslationSection(): void {
    const t = this.store.get('translation') as Partial<AppConfig['translation']> | undefined;
    if (t && t.model === undefined) {
      this.store.set('translation', { ...CONFIG_DEFAULTS.translation, ...t });
    }
  }

  /**
   * asr 段规范化（幂等；settings-management spec「Whisper 档位偏好迁移」）：
   * 旧 store 的 Whisper 模型档位字段（`asr.model`，如 'base'）随引擎替换
   * 一并移除，改写为 `{ language }`（非法/缺失语言回退默认 ja，日志记录迁移）。
   */
  private normalizeAsrSection(): void {
    const raw = this.store.get('asr') as unknown;
    const resolved = resolveAsrSection(raw);
    if (resolved.changed) {
      const hadModel = (raw !== null && typeof raw === 'object'
        && 'model' in (raw as Record<string, unknown>));
      (this.store.set as unknown as (key: string, value: unknown) => void)(
        'asr', { language: resolved.language }
      );
      if (hadModel) {
        // 迁移日志（spec 场景：升级首启移除档位字段并初始化源语言）
        console.info('[config] asr.model（Whisper 档位）已移除，源语言偏好初始化为', resolved.language);
      }
    }
  }

  /**
   * 快捷键段清理（幂等）：移除随模型档位废弃的 `switchModel` 键（Ctrl+Shift+M）
   * ——specs: overlay-window「快捷键完整注册」。残留键不再注册、不展示。
   */
  private pruneLegacyShortcutKeys(): void {
    const shortcuts = this.store.get('shortcuts') as unknown;
    const status = this.store.get('shortcutStatus') as unknown;
    const sRec = (shortcuts !== null && typeof shortcuts === 'object' ? shortcuts : null) as Record<string, unknown> | null;
    const stRec = (status !== null && typeof status === 'object' ? status : null) as Record<string, unknown> | null;
    if (sRec && 'switchModel' in sRec) {
      const { switchModel: _removed, ...rest } = sRec;
      this.store.set('shortcuts', rest as unknown as AppConfig['shortcuts']);
    }
    if (stRec && 'switchModel' in stRec) {
      const { switchModel: _removed, ...rest } = stRec;
      this.store.set('shortcutStatus', rest as unknown as AppConfig['shortcutStatus']);
    }
  }

  get<K extends keyof AppConfig>(key: K): AppConfig[K] {
    return this.store.get(key);
  }

  set<K extends keyof AppConfig>(key: K, value: AppConfig[K]): void {
    this.store.set(key, value);
  }

  /**
   * 点路径写入（如 'subtitle.fontSize'、'system.autoStart'）。
   * 路径必须来自调用方白名单校验（WRITABLE_CONFIG_PATHS），本方法不做语义判断。
   */
  setByPath(path: string, value: unknown): void {
    (this.store.set as unknown as (k: string, v: unknown) => void)(path, value);
  }

  get all(): AppConfig {
    return this.store.store;
  }

  /** 订阅偏好变化（Wave C 用于触发 Gateway 对齐与窗口广播） */
  onDidChange<K extends keyof AppConfig>(
    key: K, cb: (newValue?: AppConfig[K], oldValue?: AppConfig[K]) => void
  ): () => void {
    return this.store.onDidChange(key, cb);
  }

  /**
   * 一次性执行旧 config.yaml 偏好段迁移（幂等）。
   * 只读原文件，绝不重写（spec "旧配置迁移" 场景）。
   */
  migrateLegacyYaml(configYamlPath: string, logger: BackendLogger): { applied: number } | null {
    const setByPath: SetByPath = (key, value) => {
      // electron-store 运行时支持点路径；此处放宽类型仅为该边界
      (this.store.set as unknown as SetByPath)(key, value);
    };
    return runLegacyYamlMigration({
      configYamlPath,
      isMigrated: () => this.store.get('legacyMigrated'),
      markMigrated: () => this.store.set('legacyMigrated', true),
      applyEntry: (entry: MigrationEntry) => setByPath(entry.key, entry.value),
      logger
    });
  }
}
