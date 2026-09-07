import type { RunEvent, StreamFrame } from "@/lib/types";

/**
 * One rendered row of the run timeline.
 *
 * Both the persisted event log and the live SSE stream are normalised into this
 * shape so the timeline component never has to know which source it is reading.
 */
export interface TimelineItem {
  id: string;
  seq: number;
  agent: string;
  type: string;
  payload: Record<string, unknown>;
  createdAt: string | null;
}

export function fromRunEvent(event: RunEvent): TimelineItem {
  return {
    id: event.id,
    seq: event.seq,
    agent: event.agent,
    type: event.event_type,
    payload: event.payload,
    createdAt: event.created_at,
  };
}

export function fromStreamFrame(frame: StreamFrame): TimelineItem {
  return {
    id: frame.id,
    seq: frame.seq,
    agent: frame.agent,
    type: frame.type,
    payload: frame.payload,
    createdAt: frame.created_at,
  };
}