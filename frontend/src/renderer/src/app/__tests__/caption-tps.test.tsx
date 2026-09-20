// @vitest-environment jsdom
/**
 * 直播字幕卡片 LLM 生成速度脚注（add-llm-tps-to-subtitles 3.3）：
 * 有 tps 显示「46.5 tok/s」，无 tps 不出现速度文本。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字，禁止 hex/rgb。
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { CaptionCard } from '../LivePage';
import type { CaptionEntry } from '../../state/hooks';

afterEach(cleanup);

function entry(tps?: number): CaptionEntry {
  const msg: SubtitleMessageView = {
    type: 'subtitle',
    original: '今日は天気がいいですね',
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: '今天天气真好啊' }
  };
  if (tps !== undefined) msg.tps = tps;
  return {
    seq: 1,
    receivedAt: new Date(2026, 0, 2, 14, 32, 5).getTime(),
    msg
  };
}

describe('CaptionCard 生成速度脚注', () => {
  it('有 tps → 脚注显示 1 位小数速度', () => {
    render(
      <CaptionCard entry={entry(46.47)} displayMode="original_and_translation" />
    );
    expect(screen.getByText(/46\.5 tok\/s/)).not.toBeNull();
  });

  it('无 tps → 不显示速度文本（无占位符）', () => {
    render(<CaptionCard entry={entry()} displayMode="original_and_translation" />);
    expect(screen.queryByText(/tok\/s/)).toBeNull();
  });
});
