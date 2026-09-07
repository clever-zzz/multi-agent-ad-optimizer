import type { ReactNode } from "react";
import { cn } from "@/lib/cn";
import { Skeleton } from "@/components/ui/Skeleton";
import { EmptyState } from "@/components/ui/EmptyState";

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T, index: number) => ReactNode;
  className?: string;
  headerClassName?: string;
  align?: "left" | "right" | "center";
}

const ALIGN = {
  left: "text-left",
  right: "text-right",
  center: "text-center",
} as const;

export interface TableProps<T> {
  columns: Array<Column<T>>;
  rows: T[];
  rowKey: (row: T, index: number) => string;
  loading?: boolean;
  error?: ReactNode;
  emptyTitle?: string;
  emptyHint?: ReactNode;
  emptyAction?: ReactNode;
  onRowClick?: (row: T) => void;
  skeletonRows?: number;
  dense?: boolean;
  className?: string;
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  loading = false,
  error,
  emptyTitle = "Nothing here yet",
  emptyHint,
  emptyAction,
  onRowClick,
  skeletonRows = 6,
  dense = false,
  className,
}: TableProps<T>) {
  const pad = dense ? "px-3 py-1.5" : "px-3 py-2.5";

  if (error) {
    return (
      <EmptyState
        tone="negative"
        icon="warning"
        title="Could not load data"
        hint={error}
        action={emptyAction}
      />
    );
  }

  return (
    <div className={cn("w-full overflow-x-auto", className)}>
      <table className="w-full border-collapse text-[13px]">
        <thead>
          <tr className="border-b border-line bg-surface-2/60">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cn(
                  pad,
                  "text-[11px] font-semibold tracking-wider text-ink-3 uppercase whitespace-nowrap",
                  ALIGN[column.align ?? "left"],
                  column.headerClassName,
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading && rows.length === 0
            ? Array.from({ length: skeletonRows }).map((_, index) => (
                <tr key={`skeleton-${index}`} className="border-b border-line/60">
                  {columns.map((column) => (
                    <td key={column.key} className={pad}>
                      <Skeleton className="h-4 w-full" />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((row, index) => (
                <tr
                  key={rowKey(row, index)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={cn(
                    "border-b border-line/60 transition-colors last:border-b-0",
                    onRowClick && "cursor-pointer hover:bg-surface-2/70",
                  )}
                >
                  {columns.map((column) => (
                    <td
                      key={column.key}
                      className={cn(pad, "align-middle text-ink-1", ALIGN[column.align ?? "left"], column.className)}
                    >
                      {column.cell(row, index)}
                    </td>
                  ))}
                </tr>
              ))}
        </tbody>
      </table>
      {!loading && rows.length === 0 && (
        <EmptyState title={emptyTitle} hint={emptyHint} action={emptyAction} />
      )}
    </div>
  );
}