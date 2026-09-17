import type { ReactNode } from 'react';
import { cx } from './cx';

export interface SegmentedNavItem {
  value: string;
  label: string;
  icon?: ReactNode;
}

export interface SegmentedNavProps {
  items: SegmentedNavItem[];
  value: string;
  onChange(value: string): void;
  direction?: 'horizontal' | 'vertical';
}

/** 分段导航（设置页左侧 / 顶部 Tab） */
export function SegmentedNav({
  items, value, onChange, direction = 'horizontal'
}: SegmentedNavProps) {
  return (
    <nav
      aria-label="分段导航"
      className={cx(
        'flex gap-1',
        direction === 'vertical' ? 'flex-col' : 'flex-row items-center'
      )}
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            type="button"
            aria-current={active ? 'true' : undefined}
            onClick={() => onChange(item.value)}
            className={cx(
              'inline-flex items-center gap-2 rounded-control px-3 py-2 text-base',
              'transition-colors duration-normal ease-app',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              direction === 'vertical' && 'w-full justify-start',
              active
                ? 'bg-elevated text-primary'
                : 'text-secondary hover:bg-surface-hover hover:text-primary'
            )}
          >
            {item.icon}
            {item.label}
          </button>
        );
      })}
    </nav>
  );
}
