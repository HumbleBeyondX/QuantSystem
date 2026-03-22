// ---------------------------------------------------------------------------
// Shared TypeScript types & interfaces for the QuantSystem dashboard
// ---------------------------------------------------------------------------

/** High-level portfolio snapshot returned by the /portfolio/summary endpoint. */
export interface PortfolioSummary {
  total_value: number;
  cash: number;
  pnl: number;
  pnl_pct: number;
  positions_count: number;
  daily_pnl: number;
  daily_pnl_pct: number;
}

/** A single open position within the portfolio. */
export interface Position {
  symbol: string;
  quantity: number;
  avg_price: number;
  current_price: number;
  market_value: number;
  unrealized_pnl: number;
  pnl_pct: number;
  asset_class: string;
  strategy_id: string;
}

/** One data-point on the equity curve / drawdown chart. */
export interface EquityCurvePoint {
  timestamp: string;
  equity: number;
  drawdown: number;
}

/** Running strategy descriptor. */
export type StrategyState = "running" | "paused" | "stopped";

export interface StrategyInfo {
  id: string;
  name: string;
  asset_class: string;
  state: StrategyState;
  pnl: number;
  win_rate: number;
  total_trades: number;
  sharpe: number;
}

/** Real-time or historical signal emitted by a strategy. */
export type SignalDirection = "LONG" | "SHORT" | "FLAT";

export interface Signal {
  id: string;
  strategy_id: string;
  symbol: string;
  direction: SignalDirection;
  strength: number;
  timestamp: string;
}

/** Aggregated metrics produced by a back-test run. */
export interface BacktestMetrics {
  total_return: number;
  annual_return: number;
  sharpe: number;
  sortino: number;
  max_drawdown: number;
  win_rate: number;
  profit_factor: number;
  total_trades: number;
  calmar: number;
}

/** Back-test run descriptor (references its metrics). */
export interface BacktestResult {
  id: string;
  strategy_id: string;
  start_date: string;
  end_date: string;
  metrics: BacktestMetrics;
}

/** Portfolio-level risk summary. */
export interface RiskSummary {
  portfolio_var: number;
  portfolio_cvar: number;
  current_drawdown: number;
  max_drawdown: number;
  leverage: number;
  correlation_alert: boolean;
}

/** Options Greeks exposure for the whole book. */
export interface GreeksExposure {
  delta: number;
  gamma: number;
  theta: number;
  vega: number;
  rho: number;
}

/** A single OHLCV bar for charting. */
export interface OHLCVBar {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** One level of an order-book snapshot. */
export interface OrderBookEntry {
  price: number;
  quantity: number;
  total: number;
}

/** Status of an individual micro-service or dependency. */
export interface ServiceStatus {
  name: string;
  status: "up" | "down";
  latency_ms: number;
  last_check: string;
}

/** Aggregate system health reported by the monitoring endpoint. */
export type HealthStatus = "healthy" | "degraded" | "down";

export interface SystemHealth {
  status: HealthStatus;
  services: Record<string, ServiceStatus>;
}

/** Generic envelope for messages arriving over the WebSocket connection. */
export interface WebSocketMessage {
  type: string;
  data: unknown;
  timestamp: string;
}
