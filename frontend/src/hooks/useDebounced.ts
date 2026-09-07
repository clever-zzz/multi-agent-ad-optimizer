import { useEffect, useState } from "react";

// Search boxes drive server-side filters, so keystrokes must not each become a
// request. The first value is returned immediately to keep the UI responsive.
export function useDebounced<T>(value: T, delayMs = 300): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    if (value === debounced) return;
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs, debounced]);

  return debounced;
}