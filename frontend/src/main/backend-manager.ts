/**
 * BackendManager — 后端进程托管（纯 Node 模块，不 import electron）
 *
 * 职责（release-packaging spec "后端进程托管"）：
 * - spawn 后端（dev: python main.py；打包: SubtitleTranslator.exe）
 * - 端口探活健康检查（指数退避 250ms→4s，默认上限 90s）
 * - 退出回收（taskkill 进程树）、重启
 * - 用户 config.yaml 副本保障 + 环境变量注入（SUBTITLE_LOG_DIR/SUBTITLE_CONFIG_PATH）
 *
 * 路径与依赖全部注入，可独立单测；Wave C 在 index.ts 中以 electron app 信息装配。
 */
import * as fs from 'fs';
import * as net from 'net';
import * as path from 'path';
import { spawn, type ChildProcess } from 'child_process';

export interface BackendLogger {
  info(...args: unknown[]): void;
  warn(...args: unknown[]): void;
  error(...args: unknown[]): void;
}

export interface BackendPaths {
  isPackaged: boolean;
  /** app.isPackaged 时的 process.resourcesPath */
  resourcesPath: string;
  /** dev 模式后端目录（repo/backend） */
  devBackendDir: string;
  /** dev 模式配置模板（repo/config.yaml） */
  devConfigTemplate: string;
  /** app.getPath('userData') */
  userDataDir: string;
}

export interface BackendEndpoint {
  host: string;
  port: number;
}

export interface SpawnSpec {
  cmd: string;
  args: string[];
  cwd?: string;
}

export type SpawnFn = (
  cmd: string, args: string[], opts: { cwd?: string; env: NodeJS.ProcessEnv }
) => ChildProcess;

export type ProbeFn = (host: string, port: number) => Promise<boolean>;

/** 启动命令解析（纯函数，可测） */
export function backendSpawnSpec(paths: BackendPaths): SpawnSpec {
  if (paths.isPackaged) {
    // extraResources 结构: resources/backend/SubtitleTranslator/SubtitleTranslator.exe
    const exeDir = path.join(paths.resourcesPath, 'backend', 'SubtitleTranslator');
    return { cmd: path.join(exeDir, 'SubtitleTranslator.exe'), args: [], cwd: exeDir };
  }
  return { cmd: 'python', args: ['main.py'], cwd: paths.devBackendDir };
}

/** 探活退避：delay → min(floor(delay*1.5), 4000) */
export function nextProbeDelay(current: number): number {
  return Math.min(Math.floor(current * 1.5), 4000);
}

/** 首次运行把默认 config.yaml 复制到用户数据目录，此后以副本为准（不覆盖） */
export function ensureConfigCopy(
  userDataDir: string, templatePath: string, logger: BackendLogger
): string {
  const userConfig = path.join(userDataDir, 'config.yaml');
  if (!fs.existsSync(userConfig)) {
    try {
      fs.mkdirSync(userDataDir, { recursive: true });
      fs.copyFileSync(templatePath, userConfig);
      logger.info('已生成用户配置副本:', userConfig);
    } catch (err) {
      logger.error('复制默认配置失败:', err);
    }
  }
  return userConfig;
}

/** TCP 端口探活（默认实现） */
export function probeTcp(host: string, port: number): Promise<boolean> {
  return new Promise<boolean>((resolve) => {
    const sock = net.connect(port, host === 'localhost' ? '127.0.0.1' : host);
    sock.once('connect', () => { sock.destroy(); resolve(true); });
    sock.once('error', () => { sock.destroy(); resolve(false); });
  });
}

export interface BackendManagerHooks {
  /** 后端进程退出（非重启流程）：Wave C 接托盘气泡 + AppState */
  onExit?(code: number | null): void;
}

export interface BackendManagerInject {
  spawnFn?: SpawnFn;
  probeFn?: ProbeFn;
}

export class BackendManager {
  private proc: ChildProcess | null = null;
  private restarting = false;
  private readonly spawnFn: SpawnFn;
  private readonly probeFn: ProbeFn;

  constructor(
    private readonly paths: BackendPaths,
    private readonly endpoint: BackendEndpoint,
    private readonly logger: BackendLogger,
    private readonly hooks: BackendManagerHooks = {},
    inject: BackendManagerInject = {}
  ) {
    this.spawnFn = inject.spawnFn ?? (spawn as unknown as SpawnFn);
    this.probeFn = inject.probeFn ?? probeTcp;
  }

  get running(): boolean {
    return this.proc !== null;
  }

  /** 拉起后端（幂等） */
  start(): void {
    if (this.proc) return;

    const spec = backendSpawnSpec(this.paths);
    const logDir = path.join(this.paths.userDataDir, 'logs');
    fs.mkdirSync(logDir, { recursive: true });
    const template = this.paths.isPackaged
      ? path.join(this.paths.resourcesPath, 'config.yaml')
      : this.paths.devConfigTemplate;

    const env: NodeJS.ProcessEnv = {
      ...process.env,
      SUBTITLE_LOG_DIR: logDir,
      SUBTITLE_CONFIG_PATH: ensureConfigCopy(this.paths.userDataDir, template, this.logger)
    };

    this.logger.info('启动后端:', spec.cmd, spec.args.join(' '), spec.cwd ?? '');
    try {
      this.proc = this.spawnFn(spec.cmd, spec.args, { cwd: spec.cwd, env });
    } catch (err) {
      this.logger.error('后端启动失败:', err);
      this.proc = null;
      return;
    }

    const proc = this.proc;
    proc.stderr?.on('data', (data: Buffer) => {
      this.logger.error('[backend]', data.toString().trim());
    });
    proc.stdout?.on('data', (data: Buffer) => {
      this.logger.info('[backend]', data.toString().trim());
    });
    proc.on('exit', (code) => {
      this.logger.error(`后端进程退出，代码 ${code}`);
      if (this.proc === proc) this.proc = null;
      if (!this.restarting) this.hooks.onExit?.(code);
    });
    proc.on('error', (err) => {
      this.logger.error('后端进程错误:', err);
      if (this.proc === proc) this.proc = null;
    });
  }

  /** 结束后端进程树（Windows: taskkill /T /F） */
  stop(): void {
    const pid = this.proc?.pid;
    this.proc = null;
    if (pid) {
      try {
        this.spawnFn('taskkill', ['/pid', String(pid), '/T', '/F'], {
          cwd: undefined, env: process.env
        });
      } catch (err) {
        this.logger.error('taskkill 失败:', err);
      }
    }
  }

  /** 重启（stop → 1s → start），期间抑制 onExit 钩子 */
  async restart(): Promise<void> {
    this.restarting = true;
    this.stop();
    await new Promise((r) => setTimeout(r, 1000));
    this.start();
    this.restarting = false;
  }

  /** 轮询端口直到后端就绪（指数退避，默认上限 90s） */
  async waitForReady(timeoutMs = 90000): Promise<boolean> {
    const { host, port } = this.endpoint;
    const deadline = Date.now() + timeoutMs;
    let delay = 250;

    while (Date.now() < deadline) {
      const ok = await this.probeFn(host, port);
      if (ok) {
        this.logger.info('后端健康检查通过');
        return true;
      }
      await new Promise((r) => setTimeout(r, delay));
      delay = nextProbeDelay(delay);
    }

    this.logger.error('后端健康检查超时');
    return false;
  }
}
