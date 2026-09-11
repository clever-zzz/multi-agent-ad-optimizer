import { useI18n } from "@/i18n";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";
import { formatNumber } from "@/lib/format";

export interface PaginationProps {
  page: number;
  pageSize: number;
  total: number;
  onChange: (page: number) => void;
  onPageSizeChange?: (size: number) => void;
  pageSizeOptions?: number[];
  className?: string;
}

export function Pagination({
  page,
  pageSize,
  total,
  onChange,
  onPageSizeChange,
  pageSizeOptions = [20, 50, 100, 200],
  className,
}: PaginationProps) {
  const { t } = useI18n();
  const pageCount = pageSize > 0 ? Math.max(1, Math.ceil(total / pageSize)) : 1;
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(total, page * pageSize);

  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-between gap-3 border-t border-line px-4 py-2.5",
        className,
      )}
    >
      <p className="tnum text-xs text-ink-3">
        {t("{from}–{to} of {total}", { from: formatNumber(from), to: formatNumber(to), total: formatNumber(total) })}
      </p>
      <div className="flex items-center gap-2">
        {onPageSizeChange && (
          <label className="flex items-center gap-1.5 text-xs text-ink-3">
            {t("Rows")}
            <select
              value={pageSize}
              onChange={(event) => onPageSizeChange(Number(event.target.value))}
              className="field h-7 w-auto py-0 text-xs"
            >
              {pageSizeOptions.map((size) => (
                <option key={size} value={size}>
                  {size}
                </option>
              ))}
            </select>
          </label>
        )}
        <Button
          size="xs"
          variant="ghost"
          icon="chevronLeft"
          disabled={page <= 1}
          onClick={() => onChange(page - 1)}
          aria-label={t("Previous page")}
        />
        <span className="tnum min-w-16 text-center text-xs text-ink-2">
          {page} / {pageCount}
        </span>
        <Button
          size="xs"
          variant="ghost"
          iconRight="chevronRight"
          disabled={page >= pageCount}
          onClick={() => onChange(page + 1)}
          aria-label={t("Next page")}
        />
      </div>
    </div>
  );
}