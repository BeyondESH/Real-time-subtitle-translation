/**
 * HistoryStore 真库测试（session-history spec 场景）
 *
 * better-sqlite3 是原生模块：当本地 binding 与 Node ABI 不符
 * （例如刚为 Electron 重编译过）时，动态 import 失败 → 显式跳过并告警，
 * 不静默（CI/开发时 npm rebuild better-sqlite3 可恢复真实执行）。
 */
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { defaultSessionTitle, snippetAround } from '../history-util';

let HistoryStoreMod: typeof import('../history-db') | null = null;
let loadError: string | null = null;
try {
  const mod = await import('../history-db');
  // 探针：binding 可能在 import 时正常、实例化时 DLOPEN 失败（ABI 不匹配）
  const probe = new mod.HistoryStore(':memory:', {
    info: () => undefined, warn: () => undefined, error: () => undefined
  });
  probe.dispose();
  HistoryStoreMod = mod;
} catch (e) {
  loadError = String(e);
  console.warn(
    '[history-db.test] better-sqlite3 原生绑定在本运行时不可用，跳过真库测试（原因见下）。'
    + '当前绑定为 Electron ABI 时属预期；恢复 Node 侧执行：npm rebuild better-sqlite3\n',
    loadError
  );
}

const logger = {
  info: () => undefined,
  warn: () => undefined,
  error: () => undefined
};

function makeInput(over: Partial<import('../history-db').InsertUtteranceInput> = {}) {
  return {
    receivedAt: Date.now(),
    tsStart: 1.0,
    tsEnd: 3.5,
    original: 'こんにちは',
    sourceLang: 'ja',
    translation: '你好',
    targetLang: 'zh',
    model: 'base',
    ...over
  };
}

describe.skipIf(!HistoryStoreMod)('HistoryStore（真库）', () => {
  let tmpDir: string;
  let store: InstanceType<NonNullable<typeof HistoryStoreMod>['HistoryStore']>;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hist-'));
    store = new HistoryStoreMod!.HistoryStore(path.join(tmpDir, 'history.db'), logger);
  });

  afterEach(() => {
    store.dispose();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('启动会话：空会话复用，有语句后自动开新会话', () => {
    const id1 = store.ensureActiveSession(1000, 'Speakers');
    const id2 = store.ensureActiveSession(2000, 'Speakers');
    expect(id2).toBe(id1); // 空会话不堆积

    store.insertUtterance(makeInput());
    const id3 = store.ensureActiveSession(3000, 'Speakers');
    expect(id3).not.toBe(id1); // 已有语句 → 新会话
    expect(store.listSessions().length).toBe(2);
  });

  it('首句落库自动改标题（译文前 12 字）', () => {
    const id = store.ensureActiveSession(1000, '');
    const long = '这是一条非常非常长的译文用于验证标题截断行为';
    store.insertUtterance(makeInput({ translation: long }));
    const s = store.getSession(id);
    expect(s?.title).toBe(long.slice(0, 12));
    // 第二句不再改标题
    store.insertUtterance(makeInput({ translation: '第二句' }));
    expect(store.getSession(id)?.title).toBe(long.slice(0, 12));
  });

  it('手动新建会话：封口旧会话 ended_at 并切换活跃', () => {
    const id1 = store.ensureActiveSession(1000, '');
    store.insertUtterance(makeInput());
    const id2 = store.newSession(50_000, '');
    const s1 = store.getSession(id1);
    expect(s1?.ended_at).toBe(50_000);
    expect(store.getActiveId()).toBe(id2);
  });

  it('无活跃会话时写入返回 -1，不抛出', () => {
    store.closeActiveSession(Date.now());
    expect(store.insertUtterance(makeInput())).toBe(-1);
  });

  it('静音自动切分：关闭=不切；开启且超阈值=切', () => {
    store.ensureActiveSession(1000, '');
    store.insertUtterance(makeInput({ receivedAt: 1000 }));
    // 关闭
    expect(store.maybeAutoSplit(1000 + 60 * 60_000, null, '')).toBe(false);
    // 29 分钟：不切
    expect(store.maybeAutoSplit(1000 + 29 * 60_000, 30, '')).toBe(false);
    // 31 分钟：切
    expect(store.maybeAutoSplit(1000 + 31 * 60_000, 30, '')).toBe(true);
    expect(store.listSessions().length).toBe(2);
  });

  it('重命名：空标题拒绝，超长截断', () => {
    const id = store.ensureActiveSession(1000, '');
    expect(store.renameSession(id, '   ')).toBe(false);
    expect(store.renameSession(id, '生肉直播')).toBe(true);
    expect(store.getSession(id)?.title).toBe('生肉直播');
    const longTitle = 'x'.repeat(100);
    store.renameSession(id, longTitle);
    expect(store.getSession(id)?.title?.length).toBe(60);
  });

  it('删除会话级联删语句；删除活跃会话返回 wasActive', () => {
    const id = store.ensureActiveSession(1000, '');
    store.insertUtterance(makeInput());
    expect(store.listUtterances(id).length).toBe(1);
    const r = store.deleteSession(id);
    expect(r).toEqual({ deleted: true, wasActive: true });
    expect(store.getSession(id)).toBeNull();
    expect(store.listUtterances(id).length).toBe(0);
    expect(store.getActiveId()).toBeNull();
  });

  it('搜索：原文/译文命中、不区分大小写、空查询返回空', () => {
    store.ensureActiveSession(1000, '');
    store.insertUtterance(makeInput({ original: 'Hello World', translation: '你好世界' }));
    store.insertUtterance(makeInput({ original: 'Good morning', translation: '早上好' }));

    expect(store.search('world').length).toBe(1); // 大小写不敏感
    expect(store.search('你好').length).toBe(1);
    const hit = store.search('morning')[0];
    expect(hit.matchedField).toBe('original');
    expect(hit.snippet).toContain('Good morning');
    expect(hit.sessionTitle.length).toBeGreaterThan(0);
    expect(store.search('  ').length).toBe(0);
    expect(store.search('不存在的词').length).toBe(0);
  });

  it('stats 与 clearAll：统计正确，清空后收缩并可继续记录', () => {
    const id = store.ensureActiveSession(1000, '');
    for (let i = 0; i < 5; i++) store.insertUtterance(makeInput());
    let st = store.stats();
    expect(st).toMatchObject({ sessionCount: 1, utteranceCount: 5 });
    expect(st.sizeBytes).toBeGreaterThan(0);

    store.clearAll(9000, '');
    st = store.stats();
    // clearAll 开了新空会话
    expect(st.utteranceCount).toBe(0);
    expect(st.sessionCount).toBe(1);
    const newId = store.getActiveId();
    expect(newId).not.toBe(id);
    expect(store.insertUtterance(makeInput())).toBeGreaterThan(0);
  });

  it('主窗口未开时照常入库（写入不依赖任何窗口）', () => {
    // HistoryStore 为纯主进程模块，不存在窗口依赖路径；
    // 本用例锁定行为：连续写入 100 条全部可查
    store.ensureActiveSession(1000, '');
    for (let i = 0; i < 100; i++) {
      expect(store.insertUtterance(makeInput({ translation: `句子${i}` }))).toBeGreaterThan(0);
    }
    const id = store.getActiveId()!;
    expect(store.listUtterances(id).length).toBe(100);
  });

  it('exportData：映射为 ExportSession，缺时间戳保留 null', () => {
    const id = store.ensureActiveSession(1000, 'Speakers');
    store.insertUtterance(makeInput({ tsStart: 2.0, tsEnd: 4.5 }));
    store.insertUtterance(makeInput({ tsStart: null, tsEnd: null, translation: '无时间戳' }));
    // 重命名放在首句之后（自动标题只在首句落库时生效一次）
    store.renameSession(id, '导出测试');
    const data = store.exportData(id);
    expect(data).not.toBeNull();
    expect(data!.title).toBe('导出测试');
    expect(data!.utterances.length).toBe(2);
    expect(data!.utterances[0].tsStart).toBe(2.0);
    expect(data!.utterances[1].tsStart).toBeNull();
    expect(store.exportData('nonexistent')).toBeNull();
  });
});

describe('纯函数工具', () => {
  it('defaultSessionTitle 格式', () => {
    const t = defaultSessionTitle(new Date(2026, 8, 17, 9, 5).getTime());
    expect(t).toBe('09-17 09:05 会话');
  });

  it('snippetAround 以命中为中心截取', () => {
    const long = 'a'.repeat(50) + 'NEEDLE' + 'b'.repeat(50);
    const s = snippetAround(long, 'needle', 10);
    expect(s.startsWith('…')).toBe(true);
    expect(s).toContain('NEEDLE');
    expect(s.endsWith('…')).toBe(true);
  });

  it('snippetAround 未命中时截取头部（radius*2 长度）', () => {
    expect(snippetAround('hello world', 'zzz', 6)).toBe('hello world');
    expect(snippetAround('hello world', 'zzz', 3)).toBe('hello ');
  });
});
