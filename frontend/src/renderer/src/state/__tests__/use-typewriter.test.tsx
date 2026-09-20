// @vitest-environment jsdom
/**
 * useTypewriterText：译文逐字揭示（打字机）。
 * - 按节奏逐字增长；target 增长（进行中帧累积）续播不重头；
 * - 非扩展替换（退化重写）从头播放；
 * - prefers-reduced-motion / 无 matchMedia（jsdom 默认）直出全文。
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import { renderHook, act, cleanup } from '@testing-library/react';
import { useTypewriterText } from '../hooks';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** 模拟浏览器 matchMedia（jsdom 默认不提供）；reduced=true 表示降低动效偏好 */
function stubMatchMedia(reduced: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: reduced && query.includes('prefers-reduced-motion'),
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false
  }));
}

describe('useTypewriterText', () => {
  it('按节奏逐字揭示（50 字/秒）', () => {
    vi.useFakeTimers();
    stubMatchMedia(false);
    const { result } = renderHook(() => useTypewriterText('你好世界你好世界'));
    expect(result.current).toBe('');
    act(() => { vi.advanceTimersByTime(100); }); // 5 个 20ms 周期
    expect(result.current).toBe('你好世界你');
    act(() => { vi.advanceTimersByTime(200); });
    expect(result.current).toBe('你好世界你好世界');
  });

  it('target 增长（进行中帧累积）续播不重头', () => {
    vi.useFakeTimers();
    stubMatchMedia(false);
    const { result, rerender } = renderHook(
      ({ text }: { text: string }) => useTypewriterText(text),
      { initialProps: { text: '你好' } }
    );
    act(() => { vi.advanceTimersByTime(100); });
    expect(result.current).toBe('你好');
    rerender({ text: '你好世界' });
    act(() => { vi.advanceTimersByTime(100); });
    expect(result.current).toBe('你好世界');
  });

  it('非扩展替换（退化重写）从头播放', () => {
    vi.useFakeTimers();
    stubMatchMedia(false);
    const { result, rerender } = renderHook(
      ({ text }: { text: string }) => useTypewriterText(text),
      { initialProps: { text: '你好' } }
    );
    act(() => { vi.advanceTimersByTime(200); });
    expect(result.current).toBe('你好');
    rerender({ text: '早上好' });
    expect(result.current).toBe('');
    act(() => { vi.advanceTimersByTime(100); });
    expect(result.current).toBe('早上好');
  });

  it('prefers-reduced-motion 直出全文', () => {
    stubMatchMedia(true);
    const { result } = renderHook(() => useTypewriterText('你好世界'));
    expect(result.current).toBe('你好世界');
  });

  it('无 matchMedia（jsdom 默认）直出全文', () => {
    const { result } = renderHook(() => useTypewriterText('你好世界'));
    expect(result.current).toBe('你好世界');
  });
});
