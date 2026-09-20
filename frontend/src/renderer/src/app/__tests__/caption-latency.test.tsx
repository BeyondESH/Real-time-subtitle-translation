// @vitest-environment jsdom
/**
 * 直播字幕卡片端到端耗时脚注（reduce-pipeline-latency 5.3）：
 * 有 latency 显示「1.2s」（endpoint_ms + total_ms）与分解悬浮提示；缺失/非法不显示占位。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字，禁止 hex/rgb。
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { CaptionCard } from '../LivePage';
import type { CaptionEntry } from '../../state/hooks';

afterEach(cleanup);

function entry(latency?: SubtitleMessageView['latency'], tps?: number): CaptionEntry {
  const msg: SubtitleMessageView = {
    type: 'subtitle',
    original: '今日は天気がいいですね',
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: '今天天气真好啊' }
  };
  if (latency !== undefined) msg.latency = latency;
  if (tps !== undefined) msg.tps = tps;
  return {
    seq: 1,
    receivedAt: new Date(2026, 0, 2, 14, 32, 5).getTime(),
    msg
  };
}

const FULL = {
  endpoint_ms: 820,
  queue_ms: 20,
  asr_ms: 210,
  llm_ms: 190,
  total_ms: 420
};

describe('CaptionCard 端到端耗时脚注', () => {
  it('有 latency → 脚注显示说完→上屏（1 位小数秒）与分解提示', () => {
    render(
      <CaptionCard entry={entry(FULL)} displayMode="original_and_translation" />
    );
    const footer = screen.getByText(/1\.2s/);
    expect(footer).not.toBeNull();
    expect(footer.getAttribute('title')).toContain('静音等待 0.82s');
    expect(footer.getAttribute('title')).toContain('翻译 0.19s');
  });

  it('无 latency → 不显示耗时文本（无占位符）', () => {
    render(<CaptionCard entry={entry()} displayMode="original_and_translation" />);
    expect(screen.queryByText(/\d\.\ds/)).toBeNull();
  });

  it('tps 与 latency 共存', () => {
    render(
      <CaptionCard entry={entry(FULL, 46.47)} displayMode="original_and_translation" />
    );
    expect(screen.getByText(/46\.5 tok\/s/)).not.toBeNull();
    expect(screen.getByText(/1\.2s/)).not.toBeNull();
  });

  it('latency 字段非法（非有限数）→ 不显示', () => {
    const bad = { ...FULL, asr_ms: Number.NaN };
    render(<CaptionCard entry={entry(bad)} displayMode="original_and_translation" />);
    expect(screen.queryByText(/1\.2s/)).toBeNull();
  });
});
