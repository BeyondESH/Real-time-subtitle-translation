import { cx } from './cx';

export interface ToggleProps {
  checked: boolean;
  onChange(next: boolean): void;
  label?: string;
  disabled?: boolean;
}

/** 开关（role=switch，键盘可达） */
export function Toggle({ checked, onChange, label, disabled }: ToggleProps) {
  return (
    <span className="inline-flex items-center gap-3">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={cx(
          'relative inline-flex h-6 w-11 shrink-0 items-center rounded-pill border',
          'transition-colors duration-normal ease-app',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          'disabled:pointer-events-none disabled:opacity-40',
          checked ? 'border-accent bg-accent' : 'border-edge bg-elevated'
        )}
      >
        <span
          className={cx(
            'inline-block h-4 w-4 rounded-pill',
            'transition-transform duration-normal ease-app',
            checked ? 'translate-x-6 bg-onaccent' : 'translate-x-1 bg-secondary'
          )}
        />
      </button>
      {label && <span className="text-base text-primary">{label}</span>}
    </span>
  );
}
