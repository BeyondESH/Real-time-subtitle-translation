import type { ReactNode } from 'react';
import { cx } from './cx';

export interface PillProps {
  children: ReactNode;
  /** 前置图标槽 */
  icon?: ReactNode;
  /** 激活态（accent 描边+文字） */
  active?: boolean;
  /** 告警态（warn 描边+文字） */
  warn?: boolean;
  onClick?(): void;
  title?: string;
}

/**
 * 状态胶囊：无 onClick 时渲染 span（纯展示），有则渲染 button（就地切换面板触发器）
 */
export function Pill({ children, icon, active, warn, onClick, title }: PillProps) {
  const stateClass = warn
    ? 'border-warn text-warn'
    : active
      ? 'border-accent text-accent'
      : 'border-edge text-secondary hover:border-edge-strong hover:text-primary';

  const base = cx(
    'inline-flex h-7 items-center gap-1.5 rounded-pill border px-3 text-xs',
    'transition-colors duration-normal ease-app',
    stateClass
  );

  if (onClick) {
    return (
      <button
        type="button"
        title={title}
        onClick={onClick}
        className={cx(base, 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring')}
      >
        {icon}
        {children}
      </button>
    );
  }
  return (
    <span title={title} className={base}>
      {icon}
      {children}
    </span>
  );
}
