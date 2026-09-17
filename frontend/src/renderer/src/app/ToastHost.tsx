import { cx } from '../components/ui';
import { useToasts, type ToastEntry } from '../state/hooks';

const KIND_CLASS: Record<ToastEntry['kind'], string> = {
  info: 'border-edge text-primary',
  warn: 'border-warn text-warn',
  error: 'border-danger text-danger'
};

/** 右下角瞬态提示栈（切换回执 / 告警 / 错误） */
export function ToastHost() {
  const toasts = useToasts();
  if (toasts.length === 0) return null;

  return (
    <div className="pointer-events-none fixed bottom-20 right-6 z-50 flex flex-col items-end gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className={cx(
            'panel-enter rounded-control border bg-sidebar px-3 py-2 text-xs',
            KIND_CLASS[t.kind]
          )}
        >
          {t.text}
        </div>
      ))}
    </div>
  );
}
