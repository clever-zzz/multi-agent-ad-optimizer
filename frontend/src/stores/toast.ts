import { create } from "zustand";

export type ToastKind = "success" | "error" | "info" | "warning";

export interface Toast {
  id: string;
  kind: ToastKind;
  title: string;
  message?: string;
  expiresAt: number;
}

interface ToastState {
  toasts: Toast[];
  push: (kind: ToastKind, title: string, message?: string, ttlMs?: number) => string;
  dismiss: (id: string) => void;
}

let counter = 0;

export const useToasts = create<ToastState>((set, get) => ({
  toasts: [],
  push: (kind, title, message, ttlMs = 5000) => {
    counter += 1;
    const id = `t${Date.now().toString(36)}${counter}`;
    const toast: Toast = { id, kind, title, message, expiresAt: Date.now() + ttlMs };
    set({ toasts: [...get().toasts, toast].slice(-6) });
    if (ttlMs > 0) {
      setTimeout(() => get().dismiss(id), ttlMs);
    }
    return id;
  },
  dismiss: (id) => set({ toasts: get().toasts.filter((t) => t.id !== id) }),
}));

export const toast = {
  success: (title: string, message?: string) => useToasts.getState().push("success", title, message),
  error: (title: string, message?: string) => useToasts.getState().push("error", title, message, 8000),
  info: (title: string, message?: string) => useToasts.getState().push("info", title, message),
  warning: (title: string, message?: string) =>
    useToasts.getState().push("warning", title, message, 7000),
};