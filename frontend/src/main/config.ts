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
import { CONFIG_DEFAULTS, resolveAudioSection, runLegacyYamlMigration } from './config-migration';
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
