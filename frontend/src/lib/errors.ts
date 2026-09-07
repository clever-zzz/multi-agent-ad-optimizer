import { ApiError } from "@/lib/api";

/**
 * Flatten any thrown value into a single human-readable line.
 *
 * API problem documents carry per-field detail that is worth surfacing next to
 * the summary, so those are appended rather than discarded.
 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const fields = error.fieldErrors.map((item) => `${item.loc}: ${item.msg}`).join("; ");
    return fields ? `${error.message} (${fields})` : error.message;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}