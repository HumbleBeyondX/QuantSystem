// ---------------------------------------------------------------------------
// API client – thin wrapper around fetch for the QuantSystem REST API
// ---------------------------------------------------------------------------

import type {
  BacktestResult,
  EquityCurvePoint,
  GreeksExposure,
  OHLCVBar,
  OrderBookEntry,
  PortfolioSummary,
  Position,
  RiskSummary,
  Signal,
  StrategyInfo,
  SystemHealth,
} from "./types";

// ---------------------------------------------------------------------------
// Base URL – read from the Next.js public env var, fall back to localhost.
// ---------------------------------------------------------------------------

export const API_BASE: string =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

// ---------------------------------------------------------------------------
// Generic fetcher
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public readonly endpoint: string,
    public readonly status: number,
    public readonly statusText: string,
    public readonly body?: string,
  ) {
    super(`API ${status} (${statusText}) – ${endpoint}`);
    this.name = "ApiError";
  }
}

/**
 * Generic typed fetcher.
 *
 * @param endpoint  Path relative to `API_BASE`, e.g. "/portfolio/summary".
 * @param init      Optional `RequestInit` overrides (method, body, headers …).
 */
export async function fetcher<T>(
  endpoint: string,
  init?: RequestInit,
): Promise<T> {
  const url = `${API_BASE}${endpoint}`;
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });

  if (!res.ok) {
    const body = await res.text().catch(() => undefined);
    throw new ApiError(endpoint, res.status, res.statusText, body);
  }

  return res.json() as Promise<T>;
}

// ---------------------------------------------------------------------------
// Portfolio
// ---------------------------------------------------------------------------

export function fetchPortfolioSummary(): Promise<PortfolioSummary> {
  return fetcher<PortfolioSummary>("/portfolio/summary");
}

export function fetchPositions(): Promise<Position[]> {
  return fetcher<Position[]>("/portfolio/positions");
}

export function fetchEquityCurve(): Promise<EquityCurvePoint[]> {
  return fetcher<EquityCurvePoint[]>("/portfolio/equity-curve");
}

// ---------------------------------------------------------------------------
// Strategies
// ---------------------------------------------------------------------------

export function fetchStrategies(): Promise<StrategyInfo[]> {
  return fetcher<StrategyInfo[]>("/strategies");
}

export function fetchStrategy(id: string): Promise<StrategyInfo> {
  return fetcher<StrategyInfo>(`/strategies/${encodeURIComponent(id)}`);
}

export function fetchStrategySignals(id: string): Promise<Signal[]> {
  return fetcher<Signal[]>(
    `/strategies/${encodeURIComponent(id)}/signals`,
  );
}

// ---------------------------------------------------------------------------
// Back-testing
// ---------------------------------------------------------------------------

export interface BacktestParams {
  strategy_id: string;
  start_date: string;
  end_date: string;
  [key: string]: unknown;
}

export function runBacktest(params: BacktestParams): Promise<BacktestResult> {
  return fetcher<BacktestResult>("/backtests", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export function fetchBacktestResults(): Promise<BacktestResult[]> {
  return fetcher<BacktestResult[]>("/backtests");
}

export function fetchBacktestResult(id: string): Promise<BacktestResult> {
  return fetcher<BacktestResult>(`/backtests/${encodeURIComponent(id)}`);
}

// ---------------------------------------------------------------------------
// Risk
// ---------------------------------------------------------------------------

export function fetchRiskSummary(): Promise<RiskSummary> {
  return fetcher<RiskSummary>("/risk/summary");
}

export function fetchGreeksExposure(): Promise<GreeksExposure> {
  return fetcher<GreeksExposure>("/risk/greeks");
}

// ---------------------------------------------------------------------------
// Market data
// ---------------------------------------------------------------------------

export function fetchOHLCV(
  symbol: string,
  timeframe: string,
): Promise<OHLCVBar[]> {
  const params = new URLSearchParams({ symbol, timeframe });
  return fetcher<OHLCVBar[]>(`/market/ohlcv?${params.toString()}`);
}

export function fetchOrderbook(symbol: string): Promise<OrderBookEntry[]> {
  const params = new URLSearchParams({ symbol });
  return fetcher<OrderBookEntry[]>(`/market/orderbook?${params.toString()}`);
}

// ---------------------------------------------------------------------------
// System health
// ---------------------------------------------------------------------------

export function fetchSystemHealth(): Promise<SystemHealth> {
  return fetcher<SystemHealth>("/system/health");
}
