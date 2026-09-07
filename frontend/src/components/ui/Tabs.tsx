import { cn } from "@/lib/cn";

export interface TabItem<T extends string> {
  value: T;
  label: string;
  count?: number;
}

export interface TabsProps<T extends string> {
  items: Array<TabItem<T>>;
  value: T;
  onChange: (value: T) => void;
  className?: string;
  size?: "sm" | "md";
}

export function Tabs<T extends string>({
  items,
  value,
  onChange,
  className,
  size = "md",
}: TabsProps<T>) {
  return (
    <div
      role="tablist"
      className={cn(
        "flex items-center gap-1 overflow-x-auto rounded-lg border border-line bg-surface-1 p-1",
        className,
      )}
    >
      {items.map((item) => {
        const active = item.value === value;
        return (
          <button
            key={item.value}
            role="tab"
            type="button"
            aria-selected={active}
            onClick={() => onChange(item.value)}
            className={cn(
              "inline-flex items-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-colors",
              size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-[13px]",
              active
                ? "bg-surface-3 text-ink-1 shadow-sm"
                : "text-ink-3 hover:bg-surface-2 hover:text-ink-2",
            )}
          >
            {item.label}
            {item.count !== undefined && (
              <span
                className={cn(
                  "tnum rounded-full px-1.5 py-px text-[10px] font-semibold",
                  active ? "bg-brand-600/25 text-brand-300" : "bg-surface-3 text-ink-3",
                )}
              >
                {item.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}