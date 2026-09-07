import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Icon, type IconName } from "@/components/ui/Icon";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "success" | "outline";
type Size = "xs" | "sm" | "md";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-brand-600 text-white hover:bg-brand-500 active:bg-brand-600 border border-transparent shadow-sm",
  secondary:
    "bg-surface-3 text-ink-1 hover:bg-[#243044] border border-line-strong",
  ghost: "bg-transparent text-ink-2 hover:text-ink-1 hover:bg-surface-3 border border-transparent",
  outline: "bg-transparent text-ink-1 hover:bg-surface-2 border border-line-strong",
  danger: "bg-[#9f1239] text-white hover:bg-[#be123c] border border-transparent",
  success: "bg-[#047857] text-white hover:bg-[#059669] border border-transparent",
};

const SIZES: Record<Size, string> = {
  xs: "h-7 px-2 text-xs gap-1 rounded-md",
  sm: "h-8 px-3 text-[13px] gap-1.5 rounded-md",
  md: "h-9.5 px-4 text-sm gap-2 rounded-lg",
};

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: IconName;
  iconRight?: IconName;
  loading?: boolean;
  children?: ReactNode;
}

export function Button({
  variant = "secondary",
  size = "sm",
  icon,
  iconRight,
  loading = false,
  disabled,
  className,
  children,
  ...rest
}: ButtonProps) {
  return (
    <button
      type="button"
      disabled={disabled || loading}
      className={cn(
        "inline-flex items-center justify-center font-medium transition-colors",
        "disabled:opacity-45 disabled:cursor-not-allowed select-none whitespace-nowrap",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      {...rest}
    >
      {loading ? (
        <span className="size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
      ) : (
        icon && <Icon name={icon} size={size === "xs" ? 12 : 14} />
      )}
      {children}
      {iconRight && !loading && <Icon name={iconRight} size={size === "xs" ? 12 : 14} />}
    </button>
  );
}