/**
 * 导出器单测（session-history spec "多格式导出" 三场景：精确/降级/JSON 往返）
 */
import { describe, it, expect } from 'vitest';
import {
  toSrt, toTxt, toMarkdown, toJson, exportSession,
  type ExportSession, type ExportUtterance
} from '../exporters';

function utt(over: Partial<ExportUtterance> = {}): ExportUtterance {
  return {
    receivedAt: 1700000000000,
    tsStart: null,
    tsEnd: null,
    original: 'こんにちは',
    sourceLang: 'ja',
    translation: '你好',
    targetLang: 'zh',
    ...over
  };
}

function session(utterances: ExportUtterance[]): ExportSession {
  return {
    id: 'sess-1',
    title: '测试会话',
    startedAt: 1700000000000,
    endedAt: 1700000300000,
    audioSource: 'Speakers',
    utterances
  };
}

describe('SRT 导出', () => {
  it('精确时间轴：ts 归一化到首句零点，逗号毫秒格式，序号连续', () => {
    const s = session([
      utt({ tsStart: 10.0, tsEnd: 13.3, receivedAt: 1700000010000 }),
      utt({ tsStart: 15.5, tsEnd: 18.0, translation: '第二句', receivedAt: 1700000015000 })
    ]);
    const r = toSrt(s);
    expect(r.approximateTimeline).toBe(false);
    expect(r.content).toContain('1\n00:00:00,000 --> 00:00:03,300\n你好');
    expect(r.content).toContain('2\n00:00:05,500 --> 00:00:08,000\n第二句');
  });

  it('降级时间轴：缺 ts 时以 receivedAt 近似并标注', () => {
    const s = session([
      utt({ receivedAt: 1700000000000 }),
      utt({ receivedAt: 1700000004000, translation: '第二句' })
    ]);
    const r = toSrt(s);
    expect(r.approximateTimeline).toBe(true);
    // 首句句尾对齐 0 点，时长钳制 1s~5s → start 不小于 0
    expect(r.content).toMatch(/1\n00:00:00,000 --> 00:00:00,000|1\n00:00:0\d/);
    expect(r.content).toContain('2\n');
  });

  it('部分缺 ts 也走近似（全有才精确）', () => {
    const s = session([
      utt({ tsStart: 1, tsEnd: 2 }),
      utt({ receivedAt: 1700000005000 })
    ]);
    expect(toSrt(s).approximateTimeline).toBe(true);
  });
});

describe('TXT / Markdown 导出', () => {
  it('TXT 双语逐行 + 头部信息', () => {
    const s = session([utt()]);
    const r = toTxt(s);
    expect(r.content).toContain('标题: 测试会话');
    expect(r.content).toContain('[');
    expect(r.content).toContain('こんにちは');
    expect(r.content).toContain('你好');
  });

  it('原文与译文相同时不重复行', () => {
    const s = session([utt({ original: '你好', sourceLang: 'zh' })]);
    const lines = toTxt(s).content.split('\n').filter((l) => l.includes('你好'));
    expect(lines.length).toBe(1);
  });

  it('Markdown 卡片式：标题/元信息/每句小节', () => {
    const s = session([utt()]);
    const r = toMarkdown(s);
    expect(r.content).toContain('# 测试会话');
    expect(r.content).toContain('> こんにちは');
    expect(r.content).toContain('ja→zh');
  });
});

describe('JSON 导出', () => {
  it('往返一致：结构与库中记录字段对齐', () => {
    const utterances = [
      utt({ tsStart: 1.5, tsEnd: 3.25 }),
      utt({ translation: '第二句', receivedAt: 1700000004000 })
    ];
    const s = session(utterances);
    const r = toJson(s);
    const parsed = JSON.parse(r.content) as {
      session: { id: string; title: string; startedAt: number; endedAt: number | null; audioSource: string; approximateTimeline: boolean };
      utterances: ExportUtterance[];
    };
    expect(parsed.session.id).toBe('sess-1');
    expect(parsed.session.title).toBe('测试会话');
    expect(parsed.session.audioSource).toBe('Speakers');
    expect(parsed.utterances.length).toBe(2);
    expect(parsed.utterances[0].tsStart).toBe(1.5);
    expect(parsed.utterances[0].tsEnd).toBe(3.25);
    expect(parsed.utterances[0].sourceLang).toBe('ja');
    expect(parsed.utterances[1].translation).toBe('第二句');
    expect(parsed.session.approximateTimeline).toBe(true);
  });
});

describe('exportSession 分派', () => {
  it('四格式各就各位', () => {
    const s = session([utt({ tsStart: 0, tsEnd: 1 })]);
    expect(exportSession(s, 'srt').content).toContain('-->');
    expect(exportSession(s, 'txt').content).toContain('标题:');
    expect(exportSession(s, 'md').content).toContain('# ');
    expect(() => JSON.parse(exportSession(s, 'json').content)).not.toThrow();
  });
});
