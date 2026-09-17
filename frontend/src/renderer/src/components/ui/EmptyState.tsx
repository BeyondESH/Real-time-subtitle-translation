import type { ReactNode } from 'react';

export interface EmptyStateProps {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}

/** 空态（直播流无字幕、搜索无结果等） */
export function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      {icon && <div className="text-secondary">{icon}</div>}
      <div className="text-lg text-primary">{title}</div>
      {description && (
        <div className="max-w-sm text-base text-secondary">{description}</div>
      )}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
