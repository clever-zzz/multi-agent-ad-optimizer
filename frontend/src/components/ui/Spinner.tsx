import { cn } from "@/lib/cn";

export interface SpinnerProps {
  size?: number;
  label?: string;
  className?: string;
}

export function Spinner({ size = 20, label, className }: SpinnerProps) {
  return (
    <div className={cn("flex flex-col items-center gap-2.5", className)} role="status">
      <span
        className="animate-spin rounded-full border-2 border-line-strong border-t-brand-400"
        style={{ width: size, height: size }}
      />
      {label && <p className="text-xs text-ink-3">{label}</p>}
      <span className="sr-only">{label ?? "Loading"}</span>
    </div>
  );
}

export function InlineSpinner({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "inline-block size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent",
        className,
      )}
      aria-hidden="true"
    />
  );
}

export function LoadingPanel({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="grid min-h-48 place-items-center">
      <Spinner label={label} />
    </div>
  );
}