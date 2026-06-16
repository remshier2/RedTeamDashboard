// SSE wrapper around the backend's /engagements/{slug}/events feed.
//
// Uses @microsoft/fetch-event-source because the standard EventSource API
// can't send custom headers (we need the auth header). fetch-event-source
// also handles reconnect + Last-Event-ID for us.

import { fetchEventSource } from "@microsoft/fetch-event-source";
import { authHeaders } from "@/lib/api";
import { API_BASE_URL } from "@/lib/config";
import type { RunEvent } from "@/lib/types";

export interface SubscribeOptions {
  slug: string;
  thread?: string;
  onEvent: (event: RunEvent, sseId: string | undefined) => void;
  onError?: (err: unknown) => void;
  onOpen?: () => void;
  signal: AbortSignal;
  lastEventId?: string;
}

export async function subscribeToEvents(opts: SubscribeOptions): Promise<void> {
  const url = new URL(`${API_BASE_URL}/engagements/${opts.slug}/events`);
  if (opts.thread) url.searchParams.set("thread", opts.thread);

  // Resolve auth once at subscribe time; the stream stays authorized for its
  // lifetime (a fresh token is acquired on reconnect).
  const headers = {
    ...(await authHeaders()),
    ...(opts.lastEventId ? { "Last-Event-ID": opts.lastEventId } : {}),
  };

  return fetchEventSource(url.toString(), {
    method: "GET",
    headers,
    signal: opts.signal,
    openWhenHidden: true,
    onopen: async (response) => {
      if (!response.ok) {
        throw new Error(`SSE open failed: ${response.status}`);
      }
      opts.onOpen?.();
    },
    onmessage: (msg) => {
      if (!msg.data) return;
      try {
        const payload = JSON.parse(msg.data) as RunEvent;
        opts.onEvent(payload, msg.id || undefined);
      } catch {
        // ignore malformed frames
      }
    },
    onerror: (err) => {
      opts.onError?.(err);
    },
  });
}
