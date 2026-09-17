import type { MouseEvent, ReactNode } from 'react';
import { cx } from './cx';

export interface ListItemProps {
  title: string;
  subtitle?: string;
  selected?: boolean;
  onClick?(): void;
  onContextMenu?(e: MouseEvent<HTMLElement>): void;
  /** 右侧槽（徽标/按钮/时间戳） */
  trailing?: ReactNode;
}

/** 列表项（会话列表、设备列表等） */
export function ListItem({
  title, subtitle, selected, onClick, onContextMenu, trailing
}: ListItemProps) {
  const cls = cx(
    'flex w-full items-center gap-3 rounded-control px-3 py-2 text-left',
    'transition-colors duration-normal ease-app',
    selected ? 'bg-elevated' : 'hover:bg-surface-hover'
  );

  const inner = (
    <>
      <span className="min-w-0 flex-1">
        <span className={cx('block truncate text-base', selected ? 'text-primary' : 'text-primary')}>
          {title}
        </span>
        {subtitle && (
          <span className="mt-0.5 block truncate text-xs text-secondary">{subtitle}</span>
        )}
      </span>
      {trailing && <span className="shrink-0">{trailing}</span>}
    </>
  );

  if (onClick) {
    return (
      <button
        type="button"
        onClick={onClick}
        onContextMenu={onContextMenu}
        aria-current={selected ? 'true' : undefined}
        className={cx(cls, 'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring')}
      >
        {inner}
      </button>
    );
  }
  return (
    <div onContextMenu={onContextMenu} className={cls}>
      {inner}
    </div>
  );
}
