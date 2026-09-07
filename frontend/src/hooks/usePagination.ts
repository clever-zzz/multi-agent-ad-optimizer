import { useCallback, useState } from "react";

export interface PaginationState {
  page: number;
  pageSize: number;
  setPage: (page: number) => void;
  setPageSize: (pageSize: number) => void;
  reset: () => void;
}

// Page size changes must reset to page one, otherwise the new offset can land
// past the end of the result set and render an empty table.
export function usePagination(initialPageSize = 25): PaginationState {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSizeState] = useState(initialPageSize);

  const setPageSize = useCallback((next: number) => {
    setPageSizeState(next);
    setPage(1);
  }, []);

  const reset = useCallback(() => {
    setPage(1);
  }, []);

  return { page, pageSize, setPage, setPageSize, reset };
}