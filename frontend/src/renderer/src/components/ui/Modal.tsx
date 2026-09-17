import { useEffect, type ReactNode } from 'react';

export interface ModalProps {
  open: boolean;
  onClose(): void;
  title?: string;
  children: ReactNode;
  footer?: ReactNode;
}

/** 模态框：Escape 与背板点击关闭；open=false 不渲染 */
export function Modal({ open, onClose, title, children, footer }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div
        className="absolute inset-0 bg-backdrop"
        onClick={onClose}
        aria-hidden
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="relative z-10 w-full max-w-md rounded-card border border-edge bg-sidebar p-6"
      >
        {title && <h2 className="mb-4 text-lg font-semibold text-primary">{title}</h2>}
        <div className="text-base text-primary">{children}</div>
        {footer && <div className="mt-6 flex justify-end gap-3">{footer}</div>}
      </div>
    </div>
  );
}
