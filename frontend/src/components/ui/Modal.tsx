import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";
import { Icon } from "@/components/ui/Icon";

export type ModalSize = "sm" | "md" | "lg" | "xl";

const SIZES: Record<ModalSize, string> = {
  sm: "max-w-sm",
  md: "max-w-lg",
  lg: "max-w-2xl",
  xl: "max-w-4xl",
};

export interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  footer?: ReactNode;
  size?: ModalSize;
  children?: ReactNode;
  closeOnBackdrop?: boolean;
  dismissible?: boolean;
}

export function Modal({
  open,
  onClose,
  title,
  description,
  footer,
  size = "md",
  children,
  closeOnBackdrop = true,
  dismissible = true,
}: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        if (dismissible) onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panelRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [open, onClose, dismissible]);

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/65 p-4 backdrop-blur-sm sm:items-center"
      onMouseDown={(event) => {
        if (closeOnBackdrop && event.target === event.currentTarget) onClose();
      }}
      role="presentation"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        className={cn(
          "card w-full shadow-2xl shadow-black/50 outline-none",
          "animate-[modal-in_140ms_ease-out]",
          SIZES[size],
        )}
      >
        <header className="flex items-start justify-between gap-4 border-b border-line px-5 py-3.5">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-ink-1">{title}</h2>
            {description && <p className="mt-1 text-xs text-ink-3">{description}</p>}
          </div>
          {dismissible && (
            <button
              type="button"
              onClick={onClose}
              aria-label="Close dialog"
              className="-mr-1 rounded-md p-1 text-ink-3 transition-colors hover:bg-surface-3 hover:text-ink-1"
            >
              <Icon name="close" size={16} />
            </button>
          )}
        </header>
        {children && <div className="max-h-[65vh] overflow-y-auto px-5 py-4">{children}</div>}
        {footer && (
          <footer className="flex items-center justify-end gap-2 border-t border-line bg-surface-2/40 px-5 py-3">
            {footer}
          </footer>
        )}
      </div>
      <style>{`@keyframes modal-in{from{opacity:0;transform:translateY(-6px) scale(.985)}to{opacity:1;transform:none}}`}</style>
    </div>,
    document.body,
  );
}