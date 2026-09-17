export interface SliderProps {
  min: number;
  max: number;
  step?: number;
  value: number;
  onChange(value: number): void;
  label?: string;
  disabled?: boolean;
}

/** 数值滑杆（原生 range + accent token），右侧回显当前值 */
export function Slider({
  min, max, step = 1, value, onChange, label, disabled
}: SliderProps) {
  return (
    <label className="flex flex-col gap-1">
      {label && <span className="text-xs text-secondary">{label}</span>}
      <span className="flex items-center gap-3">
        <input
          type="range"
          min={min}
          max={max}
          step={step}
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(Number(e.target.value))}
          className="w-full rounded-pill accent-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40"
        />
        <span className="w-10 text-right text-xs tabular-nums text-secondary">{value}</span>
      </span>
    </label>
  );
}
