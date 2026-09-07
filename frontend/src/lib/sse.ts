import { API_BASE, API_PREFIX, getAccessToken, newRequestId } from "@/lib/api";
import type { StreamFrame } from "@/lib/types";

export interface RunStreamHandlers {
  onFrame: (frame: StreamFrame) => void;
  onClosed?: () => void;
  onError?: (error: Error) => void;
}

// EventSource cannot carry an Authorization header, and every run endpoint
// requires one, so the stream is read manually over fetch. This also gives us
// an AbortController-based teardown and lastSeq resumption.
export async function streamRun(
  runId: string,
  handlers: RunStreamHandlers,
  options: { lastSeq?: number; signal?: AbortSignal } = {},
): Promise<void> {
  const query = options.lastSeq && options.lastSeq > 0 ? `?lastSeq=${options.lastSeq}` : "";
  const token = getAccessToken();

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${API_PREFIX}/runs/${runId}/stream${query}`, {
      headers: {
        Accept: "text/event-stream",
        "X-Request-ID": newRequestId(),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      signal: options.signal,
    });
  } catch (error) {
    if ((error as Error).name !== "AbortError") handlers.onError?.(error as Error);
    return;
  }

  if (!response.ok || !response.body) {
    handlers.onError?.(new Error(`Stream failed with status ${response.status}`));
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const dispatch = (block: string): void => {
    const lines = block.split("\n");
    let eventName = "message";
    const dataLines: string[] = [];
    for (const line of lines) {
      if (line.startsWith(":")) continue; // comment / keep-alive
      if (line.startsWith("event:")) eventName = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (dataLines.length === 0) return;
    const raw = dataLines.join("\n");
    if (!raw) return;

    let frame: StreamFrame;
    try {
      const parsed = JSON.parse(raw) as Partial<StreamFrame>;
      frame = {
        id: parsed.id ?? "",
        run_id: parsed.run_id ?? runId,
        seq: parsed.seq ?? 0,
        type: parsed.type ?? eventName,
        agent: parsed.agent ?? "supervisor",
        payload: parsed.payload ?? {},
        created_at: parsed.created_at ?? new Date().toISOString(),
      };
    } catch {
      return;
    }
    if (eventName === "stream.closed") {
      handlers.onClosed?.();
      return;
    }
    handlers.onFrame(frame);
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        dispatch(buffer.slice(0, separator));
        buffer = buffer.slice(separator + 2);
        separator = buffer.indexOf("\n\n");
      }
    }
    if (buffer.trim()) dispatch(buffer);
    handlers.onClosed?.();
  } catch (error) {
    if ((error as Error).name !== "AbortError") handlers.onError?.(error as Error);
  }
}