"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchPortfolioSummary, fetchPositions } from "../lib/api";
import type { PortfolioSummary, Position } from "../lib/types";

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

/** Default polling interval in milliseconds. */
const DEFAULT_POLL_INTERVAL_MS = 5_000;

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export interface UsePortfolioReturn {
  summary: PortfolioSummary | null;
  positions: Position[];
  loading: boolean;
  error: Error | null;
  /** Manually trigger a refresh (resets the polling timer). */
  refresh: () => void;
}

/**
 * Custom hook that fetches and periodically refreshes portfolio data.
 *
 * @param pollInterval  Polling cadence in milliseconds (default 5 000 ms).
 *                      Pass `0` or `null` to disable automatic polling.
 */
export function usePortfolio(
  pollInterval: number | null = DEFAULT_POLL_INTERVAL_MS,
): UsePortfolioReturn {
  const [summary, setSummary] = useState<PortfolioSummary | null>(null);
  const [positions, setPositions] = useState<Position[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const mountedRef = useRef(true);

  // ----- data fetching -----------------------------------------------------

  const load = useCallback(async () => {
    try {
      const [summaryData, positionsData] = await Promise.all([
        fetchPortfolioSummary(),
        fetchPositions(),
      ]);

      if (!mountedRef.current) return;

      setSummary(summaryData);
      setPositions(positionsData);
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err : new Error(String(err)));
    } finally {
      if (mountedRef.current) {
        setLoading(false);
      }
    }
  }, []);

  // ----- manual refresh (also resets the polling timer) --------------------

  const refresh = useCallback(() => {
    // Clear the existing interval so the next tick starts from now.
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }

    setLoading(true);
    void load();

    // Restart polling if enabled.
    if (pollInterval && pollInterval > 0) {
      timerRef.current = setInterval(() => void load(), pollInterval);
    }
  }, [load, pollInterval]);

  // ----- lifecycle ---------------------------------------------------------

  useEffect(() => {
    mountedRef.current = true;

    // Initial fetch.
    void load();

    // Set up polling.
    if (pollInterval && pollInterval > 0) {
      timerRef.current = setInterval(() => void load(), pollInterval);
    }

    return () => {
      mountedRef.current = false;
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [load, pollInterval]);

  return { summary, positions, loading, error, refresh };
}
