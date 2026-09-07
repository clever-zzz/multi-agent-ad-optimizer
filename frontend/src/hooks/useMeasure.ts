import { useCallback, useEffect, useRef, useState } from "react";

export interface Size {
  width: number;
  height: number;
}

// Charts are drawn at real pixel dimensions rather than stretched through a
// viewBox, so stroke widths and text never distort on resize.
export function useMeasure<T extends HTMLElement>(
  initial: Size = { width: 0, height: 0 },
): [((node: T | null) => void), Size] {
  // Destructured so the callback depends on the numbers rather than on the caller's
  // object literal. Callers pass an inline default, and a fresh object each render
  // would otherwise tear down and rebuild the ResizeObserver for no reason.
  const { width: initialWidth, height: initialHeight } = initial;
  const [size, setSize] = useState<Size>(initial);
  const observerRef = useRef<ResizeObserver | null>(null);
  const nodeRef = useRef<T | null>(null);

  const ref = useCallback((node: T | null) => {
    if (nodeRef.current === node) return;

    observerRef.current?.disconnect();
    observerRef.current = null;
    nodeRef.current = node;

    if (!node) {
      setSize({ width: initialWidth, height: initialHeight });
      return;
    }

    setSize({ width: node.clientWidth, height: node.clientHeight });

    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const box = entry.contentRect;
      setSize({ width: Math.round(box.width), height: Math.round(box.height) });
    });
    observer.observe(node);
    observerRef.current = observer;
  }, [initialWidth, initialHeight]);

  useEffect(() => () => observerRef.current?.disconnect(), []);

  return [ref, size];
}