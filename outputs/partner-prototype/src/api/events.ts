import { ApiError, isRecord } from './client';

export interface ChatEvent {
  type: string; v: 1; chat_id: string; branch_id: string | null; run_id: string | null;
  seq: number; at: string; payload: Record<string, unknown>;
}
export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'closed';

/** Incremental SSE framing. Keep only one bounded, unfinished frame in memory. */
export class SseFrames {
  private pending = '';
  push(chunk: string): { event: string; id: string; data: string }[] {
    this.pending = (this.pending + chunk).replace(/\r\n/g, '\n');
    const frames: { event: string; id: string; data: string }[] = [];
    let boundary: number;
    while ((boundary = this.pending.indexOf('\n\n')) !== -1) {
      if (boundary > 262_144) throw new ApiError(502, 'stream_frame_too_large');
      const block = this.pending.slice(0, boundary);
      this.pending = this.pending.slice(boundary + 2);
      let event = 'message', id = '';
      const data: string[] = [];
      for (const line of block.split('\n')) {
        if (!line || line.startsWith(':')) continue;
        const colon = line.indexOf(':');
        const field = colon < 0 ? line : line.slice(0, colon);
        const value = colon < 0 ? '' : line.slice(colon + 1).replace(/^ /, '');
        if (field === 'event') event = value;
        if (field === 'id' && !value.includes('\0')) id = value;
        if (field === 'data') data.push(value);
      }
      if (data.length) frames.push({ event, id, data: data.join('\n') });
    }
    if (this.pending.length > 262_144) throw new ApiError(502, 'stream_frame_too_large');
    return frames;
  }
}

function decode(frame: { event: string; id: string; data: string }, chatId: string): ChatEvent {
  let value: unknown;
  try { value = JSON.parse(frame.data); } catch { throw new ApiError(502, 'invalid_stream_event'); }
  if (!isRecord(value) || value.v !== 1 || value.chat_id !== chatId || !Number.isSafeInteger(value.seq)
    || typeof value.seq !== 'number' || value.seq < 0 || String(value.seq) !== frame.id
    || typeof value.at !== 'string' || !isRecord(value.payload)
    || !(value.branch_id === null || typeof value.branch_id === 'string')
    || !(value.run_id === null || typeof value.run_id === 'string')) throw new ApiError(502, 'invalid_stream_event');
  return { type: frame.event, v: 1, chat_id: chatId, branch_id: value.branch_id,
    run_id: value.run_id, seq: value.seq, at: value.at, payload: value.payload };
}

interface Subscription {
  chatId: string;
  cursor: number;
  onEvent: (event: ChatEvent) => void;
  onReset: () => Promise<number | null>;
  onState: (state: ConnectionState) => void;
  onError: (error: ApiError) => void;
}

/** Durable cursor replay, deduplication and bounded reconnect; no UI animation. */
export function subscribeChat(options: Subscription): () => void {
  let stopped = false;
  let current: AbortController | null = null;
  let cursor = options.cursor;
  let attempt = 0;
  let wake: (() => void) | null = null;

  async function loop(): Promise<void> {
    while (!stopped) {
      options.onState(attempt ? 'reconnecting' : 'connecting');
      current = new AbortController();
      let inactivity = window.setTimeout(() => current?.abort(), 35_000);
      const touch = (): void => {
        window.clearTimeout(inactivity);
        inactivity = window.setTimeout(() => current?.abort(), 35_000);
      };
      let reset = false;
      try {
        const response = await fetch(`/api/v1/chats/${options.chatId}/events?after=${cursor}`, {
          credentials: 'same-origin', cache: 'no-store', signal: current.signal,
          headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(cursor) },
        });
        if (!response.ok) throw new ApiError(response.status, 'stream_unavailable', response.headers.get('X-Request-ID'), response.status >= 500 || response.status === 429);
        if (!response.body || !response.headers.get('Content-Type')?.includes('text/event-stream')) throw new ApiError(502, 'invalid_stream_response');
        options.onState('connected'); attempt = 0;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const frames = new SseFrames();
        try {
          while (!stopped) {
            const chunk = await reader.read();
            if (chunk.done) break;
            touch();
            for (const frame of frames.push(decoder.decode(chunk.value, { stream: true }))) {
              const event = decode(frame, options.chatId);
              if (event.type === 'stream.reset' || event.seq > cursor + 1) { reset = true; break; }
              if (event.seq <= cursor) continue;
              options.onEvent(event);
              cursor = event.seq;
            }
            if (reset) break;
          }
        } finally { await reader.cancel().catch(() => undefined); }
        if (stopped) break;
        if (reset) {
          const refreshed = await options.onReset();
          if (refreshed === null || stopped) break;
          cursor = refreshed; attempt = 0; continue;
        }
        throw new ApiError(0, 'stream_disconnected', null, true);
      } catch (failure) {
        if (stopped) break;
        const error = failure instanceof ApiError ? failure : new ApiError(0, 'stream_disconnected', null, true);
        options.onError(error);
        if ([401, 403, 404].includes(error.status)) break;
        options.onState('reconnecting');
        const milliseconds = Math.min(15_000, 1000 * 2 ** Math.min(attempt++, 4));
        await new Promise<void>((resolve) => {
          const timer = window.setTimeout(() => { wake = null; resolve(); }, milliseconds);
          wake = () => { window.clearTimeout(timer); resolve(); };
        });
      } finally { window.clearTimeout(inactivity); }
    }
    if (!stopped) options.onState('closed');
  }
  void loop();
  return () => { stopped = true; current?.abort(); wake?.(); };
}
