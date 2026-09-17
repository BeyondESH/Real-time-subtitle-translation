export interface ProgressBarProps {
  /** 0-100，自动截断 */
  value: number;
  label?: string;
  showValue?: boolean;
}

/** 进度条（模型下载等） */
export function ProgressBar({ value, label, showValue = true }: ProgressBarProps) {
  const clamped = Math.min(100, Math.max(0, Math.round(value)));
  return (
    <div className="w-full">
      {(label || showValue) && (
        <div className="mb-1.5 flex items-center justify-between text-xs text-secondary">
          {label && <span className="truncate">{label}</span>}
          {showValue && <span className="tabular-nums">{clamped}%</span>}
        </div>
      )}
      <div
        role="progressbar"
        aria-valuenow={clamped}
        aria-valuemin={0}
        aria-valuemax={100}
        className="h-2 w-full overflow-hidden rounded-pill bg-elevated"
      >
        <div
          className="h-full rounded-pill bg-accent transition-[width] duration-normal ease-app"
          style={{ width: `${clamped}%` }}
        />
      </div>
    </div>
  );
}
