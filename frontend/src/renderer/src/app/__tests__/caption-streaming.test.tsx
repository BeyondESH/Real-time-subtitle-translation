// @vitest-environment jsdom
/**
 * 直播字幕卡片渐进渲染（add-llm-streaming-output 4.5）：
 * 进行中帧弱化呈现且不显示脚注；定稿后显示脚注；first_token_ms 并入分解 title；
 * 缺失/非法不占位。
 *
 * 注：本文件在 color-scan 范围内——颜色一律用 CSS 关键字，禁止 hex/rgb。
 */
import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { CaptionCard } from '../LivePage';
import type { CaptionEntry } from '../../state/hooks';

afterEach(cleanup);

const RECEIVED = new Date(2026, 0, 2, 14, 32, 5).getTime();

function partialEntry(text = '今天'): CaptionEntry {
  return {
    seq: 1,
    receivedAt: RECEIVED,
    streaming: true,
    msg: {
      type: 'subtitle_partial',
      id: 'u1',
      original: '今日は天気がいいですね',
      source_language: 'ja',
      active_language: 'zh',
      translations: { zh: text }
    }
  };
}

function finalEntry(text: string, firstTokenMs?: number): CaptionEntry {
  const msg: SubtitleMessageView = {
    type: 'subtitle',
    original: '今日は天気がいいですね',
    source_language: 'ja',
    active_language: 'zh',
    translations: { zh: text },
    id: 'u1'
  };
  if (firstTokenMs !== undefined) msg.first_token_ms = firstTokenMs;
  return { seq: 1, receivedAt: RECEIVED, streaming: false, msg };
}

describe('CaptionCard 渐进渲染', () => {
  it('进行中帧不显示脚注（无时间戳/tps），译文弱化', () => {
    render(<CaptionCard entry={partialEntry()} displayMode="original_and_translation" />);
    const translation = screen.getByText('今天');
    expect(translation.className).toContain('text-secondary');
    expect(screen.queryByText(/\d{2}:\d{2}:\d{2}/)).toBeNull();
    expect(screen.queryByText(/tok\/s/)).toBeNull();
  });

  it('定稿后显示脚注且译文为强调样式', () => {
    render(
      <CaptionCard entry={finalEntry('今天天气真好啊')} displayMode="original_and_translation" />
    );
    const translation = screen.getByText('今天天气真好啊');
    expect(translation.className).toContain('text-primary');
    expect(screen.getByText(/14:32:05/)).not.toBeNull();
  });

  it('first_token_ms 有限数 → 分解 title 含「首字 0.31s」', () => {
    render(
      <CaptionCard
        entry={finalEntry('今天天气真好啊', 312)}
        displayMode="original_and_translation"
      />
    );
    const footer = screen.getByText(/14:32:05/);
    expect(footer.getAttribute('title')).toContain('首字 0.31s');
  });

  it('first_token_ms 缺失/非法 → title 不出现「首字」占位', () => {
    render(
      <CaptionCard entry={finalEntry('今天天气真好啊')} displayMode="original_and_translation" />
    );
    const footer = screen.getByText(/14:32:05/);
    expect(footer.getAttribute('title') ?? '').not.toContain('首字');
  });

  it('first_token_ms 非有限数 → 不显示', () => {
    render(
      <CaptionCard
        entry={finalEntry('今天天气真好啊', Number.NaN)}
        displayMode="original_and_translation"
      />
    );
    const footer = screen.getByText(/14:32:05/);
    expect(footer.getAttribute('title') ?? '').not.toContain('首字');
  });
});
