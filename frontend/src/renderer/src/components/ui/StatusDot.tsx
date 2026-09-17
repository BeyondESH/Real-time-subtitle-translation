import { cx } from './cx';

export type StatusKind = 'ok' | 'warn' | 'danger' | 'paused' | 'idle';

export interface StatusDotProps {
  status: StatusKind;
  /** 呼吸脉冲（如"聆听中"） */
  pulse?: boolean;
  label?: string;
}

const COLOR: Record<StatusKind, string> = {
  ok: 'bg-ok',
  warn: 'bg-warn',
  danger: 'bg-danger',
  paused: 'bg-secondary',
  idle: 'bg-edge-strong'
};

/** 状态点：语义色仅限 ok/warn/danger/paused/idle（design-system spec） */
export function StatusDot({ status, pulse, label }: StatusDotProps) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        aria-hidden
        data-status={status}
        className={cx('h-2 w-2 rounded-pill', COLOR[status], pulse && 'animate-pulse')}
      />
      {label && <span className="text-xs text-secondary">{label}</span>}
    </span>
  );
}
