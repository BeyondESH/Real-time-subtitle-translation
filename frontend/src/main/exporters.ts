/**
 * 导出器（session-history spec：多格式导出）
 *
 * 纯函数模块：不依赖 Electron / better-sqlite3，输入结构化数据输出字符串，vitest 全覆盖。
 * SRT 时间轴优先后端透传的 ts_start/ts_end（归一化到首句为 0 点）；
 * 缺失时以 receivedAt 近似（句尾对齐），并经 approximateTimeline 标注（UI 提示 + TXT/MD 头注）。
 */

export interface ExportUtterance {
  /** 客户端接收时刻（ms epoch） */
  receivedAt: number;
  /** 后端切句时间戳（秒，捕获时钟）；旧后端为 null */
  tsStart: number | null;
  tsEnd: number | null;
  original: string;
  sourceLang: string;
  translation: string;
  targetLang: string;
}

export interface ExportSession {
  id: string;
  title: string;
  startedAt: number;
  endedAt: number | null;
  audioSource: string;
  utterances: ExportUtterance[];
}

export interface ExportResult {
  content: string;
  /** SRT 使用了近似时间轴（缺后端时间戳） */
  approximateTimeline: boolean;
}

export type ExportFormat = 'srt' | 'txt' | 'md' | 'json';

// ---------- 时间工具 ----------

function srtTimestamp(ms: number): string {
  const clamped = Math.max(0, Math.round(ms));
  const h = Math.floor(clamped / 3600000);
  const m = Math.floor((clamped % 3600000) / 60000);
  const s = Math.floor((clamped % 60000) / 1000);
  const mil = clamped % 1000;
  const pad = (n: number, w = 2): string => String(n).padStart(w, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)},${pad(mil, 3)}`;
}

function clockLabel(msEpoch: number): string {
  const d = new Date(msEpoch);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function dateLabel(msEpoch: number): string {
  const d = new Date(msEpoch);
  const pad = (n: number): string => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 计算每条字幕的 [startMs, endMs]（会话相对时间轴） */
function computeTimeline(
  utterances: ExportUtterance[]
): { ranges: Array<[number, number]>; approximate: boolean } {
  if (utterances.length === 0) return { ranges: [], approximate: false };

  const precise = utterances.every((u) => u.tsStart !== null && u.tsEnd !== null);
  if (precise) {
    const t0 = utterances[0].tsStart as number;
    return {
      ranges: utterances.map((u) => [
        Math.max(0, ((u.tsStart as number) - t0) * 1000),
        Math.max(0, ((u.tsEnd as number) - t0) * 1000)
      ]),
      approximate: false
    };
  }

  // 近似：receivedAt 对齐句尾；时长按与上一条的间隔估算（1s~5s，首条 3s）
  const t0 = utterances[0].receivedAt;
  const ranges: Array<[number, number]> = [];
  for (let i = 0; i < utterances.length; i++) {
    const end = utterances[i].receivedAt - t0;
    const gap = i === 0 ? 3000 : utterances[i].receivedAt - utterances[i - 1].receivedAt;
    const dur = Math.min(5000, Math.max(1000, gap));
    ranges.push([Math.max(0, end - dur), Math.max(0, end)]);
  }
  return { ranges, approximate: true };
}

function header(session: ExportSession, approximateNote: boolean): string[] {
  const lines = [
    `标题: ${session.title}`,
    `开始: ${dateLabel(session.startedAt)} ${clockLabel(session.startedAt)}`,
    session.endedAt ? `结束: ${dateLabel(session.endedAt)} ${clockLabel(session.endedAt)}` : '结束: (进行中)',
    `语句数: ${session.utterances.length}`,
    session.audioSource ? `音频源: ${session.audioSource}` : ''
  ].filter((l) => l !== '');
  if (approximateNote) {
    lines.push('说明: 时间轴为接收时刻近似（来源数据缺后端时间戳）');
  }
  return lines;
}

// ---------- 四格式 ----------

export function toSrt(session: ExportSession): ExportResult {
  const { ranges, approximate } = computeTimeline(session.utterances);
  const blocks = session.utterances.map((u, i) => {
    const [start, end] = ranges[i];
    return `${i + 1}\n${srtTimestamp(start)} --> ${srtTimestamp(end)}\n${u.translation}`;
  });
  return { content: `${blocks.join('\n\n')}\n`, approximateTimeline: approximate };
}

export function toTxt(session: ExportSession): ExportResult {
  const { approximate } = computeTimeline(session.utterances);
  const lines: string[] = [...header(session, approximate), ''];
  for (const u of session.utterances) {
    if (u.original && u.original !== u.translation) {
      lines.push(`[${clockLabel(u.receivedAt)}] ${u.original}`);
    }
    lines.push(`[${clockLabel(u.receivedAt)}] ${u.translation}`);
    lines.push('');
  }
  return { content: lines.join('\n'), approximateTimeline: approximate };
}

export function toMarkdown(session: ExportSession): ExportResult {
  const { approximate } = computeTimeline(session.utterances);
  const lines: string[] = [`# ${session.title}`, ''];
  for (const h of header(session, approximate)) lines.push(`- ${h}`);
  lines.push('', '## 字幕', '');
  for (const u of session.utterances) {
    lines.push(`### ${clockLabel(u.receivedAt)} · ${u.sourceLang}→${u.targetLang}`);
    lines.push('');
    if (u.original && u.original !== u.translation) {
      lines.push(`> ${u.original}`);
      lines.push('');
    }
    lines.push(u.translation);
    lines.push('');
  }
  return { content: lines.join('\n'), approximateTimeline: approximate };
}

export function toJson(session: ExportSession): ExportResult {
  const { approximate } = computeTimeline(session.utterances);
  const payload = {
    session: {
      id: session.id,
      title: session.title,
      startedAt: session.startedAt,
      endedAt: session.endedAt,
      audioSource: session.audioSource,
      approximateTimeline: approximate
    },
    utterances: session.utterances.map((u) => ({
      receivedAt: u.receivedAt,
      tsStart: u.tsStart,
      tsEnd: u.tsEnd,
      original: u.original,
      sourceLang: u.sourceLang,
      translation: u.translation,
      targetLang: u.targetLang
    }))
  };
  return { content: JSON.stringify(payload, null, 2), approximateTimeline: approximate };
}

export function exportSession(session: ExportSession, format: ExportFormat): ExportResult {
  switch (format) {
    case 'srt': return toSrt(session);
    case 'txt': return toTxt(session);
    case 'md': return toMarkdown(session);
    case 'json': return toJson(session);
  }
}

export function fileExtension(format: ExportFormat): string {
  return format === 'md' ? 'md' : format;
}
