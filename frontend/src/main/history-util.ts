/**
 * 历史模块纯函数工具（不依赖 better-sqlite3，可独立单测）
 */

export const AUTO_TITLE_LEN = 12;
export const TITLE_MAX = 60;

/** 默认会话标题：MM-DD HH:mm 会话 */
export function defaultSessionTitle(nowMs: number): string {
  const d = new Date(nowMs);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())} 会话`;
}

/** 首句自动标题：译文前 12 个字符 */
export function autoTitleFrom(text: string): string | null {
  const t = text.trim();
  if (t.length === 0) return null;
  return t.length > AUTO_TITLE_LEN ? t.slice(0, AUTO_TITLE_LEN) : t;
}

/** 搜索命中上下文摘要：以首个命中位置为中心截取 */
export function snippetAround(text: string, query: string, radius = 30): string {
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return text.slice(0, radius * 2);
  const start = Math.max(0, idx - radius);
  const end = Math.min(text.length, idx + query.length + radius);
  return `${start > 0 ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`;
}
