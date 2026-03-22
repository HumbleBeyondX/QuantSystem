"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { WebSocketMessage } from "../lib/types";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

const WS_BASE: string =
  process.env.NEXT_PUBLIC_WS_BASE_URL ?? "ws://localhost:8000/ws";

/** Reconnect parameters (exponential back-off with jitter). */
const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 30_000;
const BACKOFF_MULTIPLIER = 2;

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export interface UseWebSocketReturn {
  /** The most recent parsed message from any subscribed channel. */
  lastMessage: WebSocketMessage | null;
  /** Whether the underlying socket is currently connected. */
  isConnected: boolean;
  /** Send an arbitrary JSON-serialisable payload to the server. */
  send: (data: unknown) => void;
}

/**
 * Custom hook that maintains a WebSocket connection to the QuantSystem backend.
 *
 * @param channels  Array of channel names to subscribe to upon connection.
 *                  Changing the array identity will trigger a re-subscribe.
 */
export function useWebSocket(channels: string[]): UseWebSocketReturn {
  const [lastMessage, setLastMessage] = useState<WebSocketMessage | null>(null);
  const [isConnected, setIsConnected] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const backoffRef = useRef(INITIAL_BACKOFF_MS);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const channelsRef = useRef(channels);
  const mountedRef = useRef(true);

  // Keep a stable reference to the latest channels list so the connect
  // function always sees the current value without triggering reconnects.
  channelsRef.current = channels;

  // ----- connect -----------------------------------------------------------

  const connect = useCallback(() => {
    // Prevent connecting when unmounted or if a socket already exists.
    if (!mountedRef.current) return;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_BASE);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!mountedRef.current) return;
      setIsConnected(true);
      backoffRef.current = INITIAL_BACKOFF_MS;

      // Subscribe to the requested channels.
      if (channelsRef.current.length > 0) {
        ws.send(
          JSON.stringify({ type: "subscribe", channels: channelsRef.current }),
        );
      }
    };

    ws.onmessage = (event: MessageEvent) => {
      if (!mountedRef.current) return;
      try {
        const parsed = JSON.parse(event.data as string) as WebSocketMessage;
        setLastMessage(parsed);
      } catch {
        // Non-JSON frame – ignore gracefully.
      }
    };

    ws.onclose = () => {
      if (!mountedRef.current) return;
      setIsConnected(false);
      wsRef.current = null;
      scheduleReconnect();
    };

    ws.onerror = () => {
      // The browser fires `onclose` after `onerror`, so reconnect is handled
      // there.  We just ensure the socket is torn down.
      ws.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ----- reconnect with exponential back-off -------------------------------

  const scheduleReconnect = useCallback(() => {
    if (!mountedRef.current) return;

    // Add a small random jitter (0 – 500 ms) to avoid thundering herds.
    const jitter = Math.random() * 500;
    const delay = Math.min(backoffRef.current + jitter, MAX_BACKOFF_MS);

    reconnectTimerRef.current = setTimeout(() => {
      backoffRef.current = Math.min(
        backoffRef.current * BACKOFF_MULTIPLIER,
        MAX_BACKOFF_MS,
      );
      connect();
    }, delay);
  }, [connect]);

  // ----- send helper -------------------------------------------------------

  const send = useCallback((data: unknown) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data));
    }
  }, []);

  // ----- lifecycle ---------------------------------------------------------

  useEffect(() => {
    mountedRef.current = true;
    connect();

    return () => {
      mountedRef.current = false;

      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }

      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  // Re-subscribe when the channel list changes while already connected.
  useEffect(() => {
    if (
      wsRef.current &&
      wsRef.current.readyState === WebSocket.OPEN &&
      channels.length > 0
    ) {
      wsRef.current.send(
        JSON.stringify({ type: "subscribe", channels }),
      );
    }
  }, [channels]);

  return { lastMessage, isConnected, send };
}
