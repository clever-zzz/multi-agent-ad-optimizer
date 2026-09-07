import type { ReactNode } from "react";
import { cn } from "@/lib/cn";

export interface PageHeaderProps {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  breadcrumb?: ReactNode;
  className?: string;
}

export function PageHeader({ title, description, actions, breadcrumb, className }: PageHeaderProps) {
  return (
    <div className={cn("mb-4 flex flex-wrap items-end justify-between gap-3", className)}>
      <div className="min-w-0">
        {breadcrumb && <div className="mb-1 flex items-center gap-1.5 text-xs text-ink-3">{breadcrumb}</div>}
        <h1 className="text-lg font-semibold tracking-tight text-ink-1">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-[13px] text-ink-3">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}