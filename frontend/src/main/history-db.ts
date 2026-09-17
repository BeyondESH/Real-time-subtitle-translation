/**
 * HistoryStore — 会话历史 SQLite 持久化（session-history spec）
 *
 * - 库文件：userData/history.db（WAL 模式）
 * - 写入路径：Gateway 收到 subtitle 即同步写入；失败记日志不抛出（不中断直播分发）
 * - 会话切分：启动自动新会话（空会话复用）/ 手动新建 / 静音超时（由调用方驱动）
 * - 搜索：LIKE 不区分大小写（SQLite ASCII 默认），带命中上下文摘要
 */
import { randomUUID } from 'crypto';
import * as fs from 'fs';
import Database from 'better-sqlite3';
import type { ExportSession, ExportUtterance } from './exporters';
import {
  TITLE_MAX, autoTitleFrom, defaultSessionTitle, snippetAround
} from './history-util';

export interface SessionRow {
  id: string;
  title: string;
  started_at: number;
  ended_at: number | null;
  audio_source: string;
  utterance_count: number;
}

export interface UtteranceRow {
  id: number;
  session_id: string;
  received_at: number;
  ts_start: number | null;
  ts_end: number | null;
  original: string;
  source_lang: string;
  translation: string;
  target_lang: string;
  model: string;
}

export interface InsertUtteranceInput {
  receivedAt: number;
  tsStart: number | null;
  tsEnd: number | null;
  original: string;
  sourceLang: string;
  translation: string;
  targetLang: string;
  model: string;
}

export interface SearchHit {
  utterance: UtteranceRow;
  sessionTitle: string;
  sessionStartedAt: number;
  /** 命中上下文摘要（原文或译文中截取的片段） */
  snippet: string;
  /** 命中字段：original | translation */
  matchedField: 'original' | 'translation';
}

export interface HistoryStats {
  sizeBytes: number;
  sessionCount: number;
  utteranceCount: number;
}

export interface HistoryLogger {
  info(...args: unknown[]): void;
  warn(...args: unknown[]): void;
  error(...args: unknown[]): void;
}

export class HistoryStore {
  private readonly db: Database.Database;
  private activeId: string | null = null;

  constructor(
    private readonly dbPath: string,
    private readonly logger: HistoryLogger
  ) {
    this.db = new Database(dbPath);
    this.db.pragma('journal_mode = WAL');
    this.db.pragma('foreign_keys = ON');
    this.migrate();
  }

  private migrate(): void {
    this.db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        started_at INTEGER NOT NULL,
        ended_at INTEGER,
        audio_source TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS utterances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        received_at INTEGER NOT NULL,
        ts_start REAL,
        ts_end REAL,
        original TEXT NOT NULL DEFAULT '',
        source_lang TEXT NOT NULL DEFAULT '',
        translation TEXT NOT NULL DEFAULT '',
        target_lang TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT ''
      );
      CREATE INDEX IF NOT EXISTS idx_utterances_session ON utterances(session_id, received_at);
      CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
    `);
  }

  // ---------- 会话生命周期 ----------

  getActiveId(): string | null {
    return this.activeId;
  }

  /** 应用启动调用：最近会话为空则复用（不堆空壳），否则开新会话 */
  ensureActiveSession(nowMs: number, audioSource: string): string {
    const last = this.db
      .prepare(`
        SELECT s.id AS id,
               (SELECT COUNT(*) FROM utterances u WHERE u.session_id = s.id) AS cnt
        FROM sessions s
        ORDER BY s.started_at DESC
        LIMIT 1
      `)
      .get() as { id: string; cnt: number } | undefined;

    if (last && last.cnt === 0) {
      this.activeId = last.id;
      return last.id;
    }
    return this.newSession(nowMs, audioSource);
  }

  newSession(nowMs: number, audioSource: string): string {
    this.closeActiveSession(nowMs);
    const id = randomUUID();
    this.db
      .prepare(
        'INSERT INTO sessions (id, title, started_at, ended_at, audio_source) VALUES (?, ?, ?, NULL, ?)'
      )
      .run(id, defaultSessionTitle(nowMs), nowMs, audioSource);
    this.activeId = id;
    return id;
  }

  closeActiveSession(nowMs: number): void {
    if (!this.activeId) return;
    this.db
      .prepare('UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL')
      .run(nowMs, this.activeId);
    this.activeId = null;
  }

  /** 静音自动切分：距上一条语句超过阈值则开新会话；返回是否发生了切分 */
  maybeAutoSplit(nowMs: number, silenceMin: number | null, audioSource: string): boolean {
    if (!silenceMin || silenceMin <= 0 || !this.activeId) return false;
    const last = this.db
      .prepare('SELECT MAX(received_at) AS t FROM utterances WHERE session_id = ?')
      .get(this.activeId) as { t: number | null };
    if (last?.t === null || last?.t === undefined) return false;
    if (nowMs - last.t < silenceMin * 60_000) return false;
    this.newSession(nowMs, audioSource);
    return true;
  }

  // ---------- 语句写入 ----------

  /** 写入单条语句；失败返回 -1（记日志，绝不抛出——直播分发优先） */
  insertUtterance(input: InsertUtteranceInput): number {
    const sessionId = this.activeId;
    if (!sessionId) return -1;
    try {
      const countRow = this.db
        .prepare('SELECT COUNT(*) AS c FROM utterances WHERE session_id = ?')
        .get(sessionId) as { c: number };
      const info = this.db
        .prepare(`
          INSERT INTO utterances
            (session_id, received_at, ts_start, ts_end, original, source_lang, translation, target_lang, model)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        `)
        .run(
          sessionId, input.receivedAt, input.tsStart, input.tsEnd,
          input.original, input.sourceLang, input.translation,
          input.targetLang, input.model
        );
      // 首句落库：默认标题改为译文前 12 字
      if (countRow.c === 0) {
        const title = autoTitleFrom(input.translation) ?? autoTitleFrom(input.original);
        if (title) {
          this.db.prepare('UPDATE sessions SET title = ? WHERE id = ?').run(title, sessionId);
        }
      }
      return Number(info.lastInsertRowid);
    } catch (err) {
      this.logger.error('历史写入失败:', err);
      return -1;
    }
  }

  // ---------- 查询 ----------

  listSessions(): SessionRow[] {
    return this.db
      .prepare(`
        SELECT s.id, s.title, s.started_at, s.ended_at, s.audio_source,
               (SELECT COUNT(*) FROM utterances u WHERE u.session_id = s.id) AS utterance_count
        FROM sessions s
        ORDER BY s.started_at DESC
      `)
      .all() as SessionRow[];
  }

  getSession(id: string): SessionRow | null {
    const row = this.db
      .prepare(`
        SELECT s.id, s.title, s.started_at, s.ended_at, s.audio_source,
               (SELECT COUNT(*) FROM utterances u WHERE u.session_id = s.id) AS utterance_count
        FROM sessions s WHERE s.id = ?
      `)
      .get(id) as SessionRow | undefined;
    return row ?? null;
  }

  listUtterances(sessionId: string): UtteranceRow[] {
    return this.db
      .prepare('SELECT * FROM utterances WHERE session_id = ? ORDER BY received_at ASC, id ASC')
      .all(sessionId) as UtteranceRow[];
  }

  renameSession(id: string, title: string): boolean {
    const clean = title.trim().slice(0, TITLE_MAX);
    if (clean.length === 0) return false;
    const info = this.db.prepare('UPDATE sessions SET title = ? WHERE id = ?').run(clean, id);
    return info.changes > 0;
  }

  /** 删除会话（级联删语句）。返回被删的是否为活跃会话 */
  deleteSession(id: string): { deleted: boolean; wasActive: boolean } {
    const wasActive = this.activeId === id;
    const info = this.db.prepare('DELETE FROM sessions WHERE id = ?').run(id);
    if (wasActive) this.activeId = null;
    return { deleted: info.changes > 0, wasActive };
  }

  search(query: string, limit = 200): SearchHit[] {
    const q = query.trim();
    if (q.length === 0) return [];
    const like = `%${q}%`;
    const rows = this.db
      .prepare(`
        SELECT u.*, s.title AS session_title, s.started_at AS session_started_at
        FROM utterances u
        JOIN sessions s ON s.id = u.session_id
        WHERE u.original LIKE ? OR u.translation LIKE ?
        ORDER BY u.received_at DESC
        LIMIT ?
      `)
      .all(like, like, limit) as Array<UtteranceRow & {
        session_title: string;
        session_started_at: number;
      }>;

    return rows.map((row) => {
      const matchedField: 'original' | 'translation' = row.translation.toLowerCase().includes(q.toLowerCase())
        ? 'translation'
        : 'original';
      return {
        utterance: {
          id: row.id,
          session_id: row.session_id,
          received_at: row.received_at,
          ts_start: row.ts_start,
          ts_end: row.ts_end,
          original: row.original,
          source_lang: row.source_lang,
          translation: row.translation,
          target_lang: row.target_lang,
          model: row.model
        },
        sessionTitle: row.session_title,
        sessionStartedAt: row.session_started_at,
        matchedField,
        snippet: snippetAround(
          matchedField === 'translation' ? row.translation : row.original, q
        )
      };
    });
  }

  stats(): HistoryStats {
    let sizeBytes = 0;
    for (const suffix of ['', '-wal', '-shm']) {
      try {
        sizeBytes += fs.statSync(`${this.dbPath}${suffix}`).size;
      } catch {
        // 文件不存在（如未产生 wal）忽略
      }
    }
    const s = this.db.prepare('SELECT COUNT(*) AS c FROM sessions').get() as { c: number };
    const u = this.db.prepare('SELECT COUNT(*) AS c FROM utterances').get() as { c: number };
    return { sizeBytes, sessionCount: s.c, utteranceCount: u.c };
  }

  /** 清空全部历史 + VACUUM 收缩，并开启新的活跃会话 */
  clearAll(nowMs: number, audioSource: string): void {
    this.activeId = null;
    this.db.exec('DELETE FROM utterances; DELETE FROM sessions;');
    this.db.exec('VACUUM');
    this.newSession(nowMs, audioSource);
  }

  /** 组装导出用数据结构（exporters 纯函数的输入） */
  exportData(sessionId: string): ExportSession | null {
    const s = this.getSession(sessionId);
    if (!s) return null;
    const utterances: ExportUtterance[] = this.listUtterances(sessionId).map((u) => ({
      receivedAt: u.received_at,
      tsStart: u.ts_start,
      tsEnd: u.ts_end,
      original: u.original,
      sourceLang: u.source_lang,
      translation: u.translation,
      targetLang: u.target_lang
    }));
    return {
      id: s.id,
      title: s.title,
      startedAt: s.started_at,
      endedAt: s.ended_at,
      audioSource: s.audio_source,
      utterances
    };
  }

  dispose(): void {
    try {
      this.db.close();
    } catch (err) {
      this.logger.warn('history.db 关闭异常:', err);
    }
  }
}
