/**
 * 主题应用（design-system spec：暗亮双主题 + 跟随系统）
 * 所有窗口共用：data-theme 属性切换，CSS 变量随之生效。
 */

export type ThemeMode = 'dark' | 'light' | 'system';

let mediaQuery: MediaQueryList | null = null;
let mediaListener: ((e: MediaQueryListEvent) => void) | null = null;
let currentMode: ThemeMode = 'dark';

function systemIsDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function applyNow(): void {
  const dark = currentMode === 'system' ? systemIsDark() : currentMode === 'dark';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}

/** 应用主题模式；system 模式下监听系统变化实时切换 */
export function applyTheme(mode: ThemeMode): void {
  currentMode = mode;

  if (mediaQuery && mediaListener) {
    mediaQuery.removeEventListener('change', mediaListener);
    mediaListener = null;
  }

  if (mode === 'system') {
    mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    mediaListener = () => applyNow();
    mediaQuery.addEventListener('change', mediaListener);
  }

  applyNow();
}
