/**
 * BackendManager 单测：spawn 规格 / 探活退避 / config 副本 / 生命周期钩子
 * （进程托管的端到端行为由 task 2.12 打包冒烟覆盖）
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { EventEmitter } from 'events';
import type { ChildProcess } from 'child_process';
import {
  backendSpawnSpec, nextProbeDelay, ensureConfigCopy, BackendManager,
  type BackendPaths
} from '../backend-manager';

class FakeChild extends EventEmitter {
  pid = 4321;
  stderr = new EventEmitter();
  stdout = new EventEmitter();
}

function fakePaths(tmpDir: string, isPackaged = false): BackendPaths {
  return {
    isPackaged,
    resourcesPath: path.join(tmpDir, 'resources'),
    devBackendDir: path.join(tmpDir, 'repo', 'backend'),
    devConfigTemplate: path.join(tmpDir, 'repo', 'config.yaml'),
    userDataDir: path.join(tmpDir, 'userData')
  };
}

const silentLogger = {
  info: () => undefined, warn: () => undefined, error: () => undefined
};

describe('backendSpawnSpec', () => {
  it('dev：python main.py + repo backend cwd', () => {
    const root = path.join('C:', 'x');
    const spec = backendSpawnSpec(fakePaths(root));
    expect(spec).toEqual({
      cmd: 'python', args: ['main.py'], cwd: path.join(root, 'repo', 'backend')
    });
  });

  it('打包：SubtitleTranslator.exe + extraResources 结构', () => {
    const spec = backendSpawnSpec(fakePaths('C:/x', true));
    expect(spec.cmd.endsWith(path.join('SubtitleTranslator.exe'))).toBe(true);
    expect(spec.cmd).toContain(path.join('resources', 'backend', 'SubtitleTranslator'));
    expect(spec.cwd).toBe(path.dirname(spec.cmd));
  });
});

describe('nextProbeDelay', () => {
  it('250 起步 1.5 倍递增，4s 封顶', () => {
    const seq: number[] = [];
    let d = 250;
    for (let i = 0; i < 10; i++) { seq.push(d); d = nextProbeDelay(d); }
    expect(seq.slice(0, 5)).toEqual([250, 375, 562, 843, 1264]);
    expect(seq.every((v) => v <= 4000)).toBe(true);
    expect(nextProbeDelay(4000)).toBe(4000);
  });
});

describe('ensureConfigCopy', () => {
  let tmpDir: string;
  beforeEach(() => { tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bm-')); });
  afterEach(() => { fs.rmSync(tmpDir, { recursive: true, force: true }); });

  it('首次复制模板；已存在则不覆盖', () => {
    const userData = path.join(tmpDir, 'ud');
    const template = path.join(tmpDir, 'template.yaml');
    fs.writeFileSync(template, 'v1', 'utf8');

    const p1 = ensureConfigCopy(userData, template, silentLogger);
    expect(fs.readFileSync(p1, 'utf8')).toBe('v1');

    fs.writeFileSync(template, 'v2', 'utf8');
    const p2 = ensureConfigCopy(userData, template, silentLogger);
    expect(p2).toBe(p1);
    expect(fs.readFileSync(p2, 'utf8')).toBe('v1'); // 不覆盖
  });

  it('模板缺失：记错误仍返回目标路径（不抛出）', () => {
    const warns: unknown[] = [];
    const p = ensureConfigCopy(
      path.join(tmpDir, 'ud2'), path.join(tmpDir, 'nope.yaml'),
      { ...silentLogger, error: (...a: unknown[]) => warns.push(a) }
    );
    expect(p.endsWith('config.yaml')).toBe(true);
    expect(warns.length).toBe(1);
  });
});

describe('BackendManager 生命周期', () => {
  let tmpDir: string;
  beforeEach(() => { tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'bmm-')); });
  afterEach(() => { fs.rmSync(tmpDir, { recursive: true, force: true }); vi.restoreAllMocks(); });

  function makeManager(
    getDevice: () => 'auto' | 'cpu' | 'cuda' = () => 'auto', packaged = false
  ) {
    fs.mkdirSync(path.join(tmpDir, 'repo'), { recursive: true });
    fs.writeFileSync(path.join(tmpDir, 'repo', 'config.yaml'), 'audio: {}', 'utf8');
    if (packaged) {
      fs.mkdirSync(path.join(tmpDir, 'resources'), { recursive: true });
      fs.writeFileSync(path.join(tmpDir, 'resources', 'config.yaml'), 'audio: {}', 'utf8');
    }
    const calls: Array<{ cmd: string; args: string[]; env: NodeJS.ProcessEnv; cwd?: string }> = [];
    const child = new FakeChild();
    const exits: Array<number | null> = [];
    const mgr = new BackendManager(
      fakePaths(tmpDir, packaged), { host: 'localhost', port: 8765 }, silentLogger,
      { onExit: (code) => exits.push(code) },
      {
        spawnFn: (cmd, args, opts) => {
          calls.push({ cmd, args, env: opts.env, cwd: opts.cwd });
          return child as unknown as ChildProcess;
        },
        probeFn: async () => true,
        getDevice
      }
    );
    return { mgr, calls, child, exits };
  }

  it('start 注入 SUBTITLE_LOG_DIR / SUBTITLE_CONFIG_PATH 环境变量', () => {
    const { mgr, calls } = makeManager();
    mgr.start();
    expect(calls.length).toBe(1);
    expect(calls[0].cmd).toBe('python');
    expect(calls[0].env.SUBTITLE_LOG_DIR).toBe(path.join(tmpDir, 'userData', 'logs'));
    expect(calls[0].env.SUBTITLE_CONFIG_PATH).toBe(path.join(tmpDir, 'userData', 'config.yaml'));
    expect(fs.existsSync(calls[0].env.SUBTITLE_CONFIG_PATH!)).toBe(true); // 副本已生成
  });

  it('dev 模式 SUBTITLE_LLAMA_DIR 指向 repo vendor 目录', () => {
    const { mgr, calls } = makeManager();
    mgr.start();
    expect(calls[0].env.SUBTITLE_LLAMA_DIR).toBe(
      path.join(tmpDir, 'repo', 'backend', 'vendor', 'llama')
    );
  });

  it('打包模式 SUBTITLE_LLAMA_DIR 指向 resources/llama', () => {
    const { mgr, calls } = makeManager(() => 'auto', true);
    mgr.start();
    expect(calls[0].cmd.endsWith('SubtitleTranslator.exe')).toBe(true);
    expect(calls[0].env.SUBTITLE_LLAMA_DIR).toBe(
      path.join(tmpDir, 'resources', 'llama')
    );
  });

  it('推理设备 auto：不注入 SUBTITLE_DEVICE', () => {
    const { mgr, calls } = makeManager(() => 'auto');
    mgr.start();
    expect(calls[0].env.SUBTITLE_DEVICE).toBeUndefined();
  });

  it('推理设备显式 cpu/cuda：注入 SUBTITLE_DEVICE', () => {
    const first = makeManager(() => 'cuda');
    first.mgr.start();
    expect(first.calls[0].env.SUBTITLE_DEVICE).toBe('cuda');

    fs.mkdirSync(path.join(tmpDir, 'repo'), { recursive: true });
    const second = makeManager(() => 'cpu');
    second.mgr.start();
    expect(second.calls[0].env.SUBTITLE_DEVICE).toBe('cpu');
  });

  it('start 幂等：重复调用只 spawn 一次', () => {
    const { mgr, calls } = makeManager();
    mgr.start();
    mgr.start();
    expect(calls.length).toBe(1);
    expect(mgr.running).toBe(true);
  });

  it('后端退出触发 onExit 钩子且 running 归位', () => {
    const { mgr, child, exits } = makeManager();
    mgr.start();
    child.emit('exit', 1);
    expect(exits).toEqual([1]);
    expect(mgr.running).toBe(false);
  });

  it('stop 以后端 pid 调 taskkill /T /F', () => {
    const { mgr, calls, child } = makeManager();
    mgr.start();
    mgr.stop();
    const kill = calls.find((c) => c.cmd === 'taskkill');
    expect(kill?.args).toEqual(['/pid', String(child.pid), '/T', '/F']);
    expect(mgr.running).toBe(false);
  });

  it('restart 期间抑制 onExit（restarting 标志）', async () => {
    const { mgr, child, exits, calls } = makeManager();
    mgr.start();
    const p = mgr.restart();
    child.emit('exit', 0); // taskkill 导致的退出
    await p;
    expect(exits).toEqual([]); // 重启流程内不上报
    expect(calls.filter((c) => c.cmd === 'python').length).toBe(2);
  });

  it('waitForReady：探活立即成功 → true', async () => {
    const { mgr } = makeManager();
    await expect(mgr.waitForReady(5000)).resolves.toBe(true);
  });
});
