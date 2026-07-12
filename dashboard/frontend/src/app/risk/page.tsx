'use client'

import { useCallback, useEffect, useState } from 'react'

/**
 * Risk Dashboard Page - Multi-strategy risk monitoring
 *
 * Features:
 * - Strategy selector dropdown to switch between monitored strategies
 * - Live risk metrics (VaR, CVaR, drawdown, leverage)
 * - Greeks exposure (options strategies)
 * - Circuit breaker status with progress bars
 * - Drawdown timeline
 * - Market regime indicators
 * - Email alert configuration
 * - Auto-refresh every 15 seconds
 */

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000'

// ── Types ────────────────────────────────────────────────────────

interface StrategyRiskOverview {
  strategy_id: string
  name: string
  asset_class: string
  status: string
  equity: number
  pnl: number
  current_dd: number
  max_dd: number
  has_position: boolean
}

interface CircuitBreaker {
  name: string
  threshold: string
  current: string
  status: 'ok' | 'warning' | 'danger'
  pct: number
}

interface RiskSummary {
  strategy_id: string
  name: string
  asset_class: string
  status: string
  equity: number
  initial_capital: number
  realized_pnl: number
  unrealized_pnl: number
  total_pnl: number
  return_pct: number
  total_trades: number
  var_95: number
  cvar_95: number
  current_dd: number
  max_dd: number
  gross_leverage: number
  net_exposure: string
  portfolio_delta: number
  portfolio_gamma: number
  portfolio_theta: number
  portfolio_vega: number
  has_greeks: boolean
  position_open: boolean
  position_detail: string
  consecutive_losses: number
  spy_price: number
  vix: number
  implied_vol: number
  rv_20d: number
  vrp: number
  circuit_breakers: CircuitBreaker[]
  drawdown_timeline: { date: string; dd: number }[]
  equity_curve: { date: string; equity: number; drawdown: number }[]
}

// ── Portfolio budgets (cross-strategy) ──
interface BudgetUtilization {
  id: string
  label: string
  used: number
  limit: number
  unit: string
  utilization: number
  breached: boolean
}

interface StressResult {
  description: string
  pnl_usd: number
  loss_usd: number
  loss_pct: number
  per_strategy_pnl_usd: Record<string, number>
  errors: string[]
}

interface BookExposure {
  strategy_id: string
  asset: string
  equity_usd: number
  position_open: boolean
  vega_usd: number
  delta_usd: number
  short_vol_notional_usd: number
  error: string | null
}

interface PortfolioRisk {
  generated_at: string
  advisory: boolean
  action_on_breach: string
  combined_equity_usd: number
  budgets: Record<string, number>
  exposures: {
    strategies: Record<string, BookExposure>
    combined_equity_usd: number
    net_vega_usd: number
    abs_vega_usd: number
    short_vol_notional_usd: number
    short_vol_notional_pct: number
    degraded: string[]
  }
  stress: Record<string, StressResult>
  utilization: BudgetUtilization[]
  breaches: string[]
  flags: Record<string, string>
  degraded: string[]
  error?: string
}

interface AlertConfig {
  email_enabled: boolean
  recipient: string
  drawdown_warning_pct: number
  drawdown_critical_pct: number
  daily_loss_warning_pct: number
  daily_loss_critical_pct: number
  vix_spike_threshold: number
  consecutive_loss_threshold: number
  leverage_warning: number
  leverage_critical: number
}

// ── Helpers ──────────────────────────────────────────────────────

const fmt = (n: number | null | undefined) =>
  (n ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })

/** Safe number: coerce null/undefined to 0 */
const N = (v: number | null | undefined): number => v ?? 0

const ASSET_BADGE: Record<string, string> = {
  options: 'bg-amber-500/20 text-amber-400',
  crypto: 'bg-purple-500/20 text-purple-400',
  equity: 'bg-blue-500/20 text-blue-400',
}

// ── Component ────────────────────────────────────────────────────

export default function RiskPage() {
  const [strategies, setStrategies] = useState<StrategyRiskOverview[]>([])
  const [selected, setSelected] = useState<string>('spy_gamma_scalp')
  const [summary, setSummary] = useState<RiskSummary | null>(null)
  const [alertConfig, setAlertConfig] = useState<AlertConfig | null>(null)
  const [portfolio, setPortfolio] = useState<PortfolioRisk | null>(null)
  const [portfolioError, setPortfolioError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showAlertPanel, setShowAlertPanel] = useState(false)
  const [testEmailStatus, setTestEmailStatus] = useState('')

  // Fetch strategy list
  const fetchStrategies = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/risk/strategies`)
      if (res.ok) setStrategies(await res.json())
    } catch {
      /* ignore */
    }
  }, [])

  // Fetch risk summary for selected strategy
  const fetchSummary = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/risk/summary?strategy_id=${selected}`)
      if (res.ok) {
        setSummary(await res.json())
        setError('')
      } else {
        setError('Failed to load risk data')
      }
    } catch {
      setError('Failed to connect to backend API')
    } finally {
      setLoading(false)
    }
  }, [selected])

  // Fetch cross-strategy portfolio budgets (slower: hits Deribit)
  const fetchPortfolio = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/risk/portfolio`)
      if (res.ok) {
        setPortfolio(await res.json())
        setPortfolioError(false)
      } else {
        setPortfolioError(true)
      }
    } catch {
      setPortfolioError(true)
    }
  }, [])

  // Fetch alert config
  const fetchAlertConfig = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/risk/alerts/config`)
      if (res.ok) setAlertConfig(await res.json())
    } catch {
      /* ignore */
    }
  }, [])

  // Initial load
  useEffect(() => {
    setLoading(true)
    fetchStrategies()
    fetchSummary()
    fetchAlertConfig()
    fetchPortfolio()
  }, [fetchStrategies, fetchSummary, fetchAlertConfig, fetchPortfolio])

  // Portfolio budgets auto-refresh (60s — endpoint fetches live greeks)
  useEffect(() => {
    const interval = setInterval(fetchPortfolio, 60000)
    return () => clearInterval(interval)
  }, [fetchPortfolio])

  // Auto-refresh
  useEffect(() => {
    const interval = setInterval(() => {
      fetchStrategies()
      fetchSummary()
    }, 15000)
    return () => clearInterval(interval)
  }, [fetchStrategies, fetchSummary])

  // Save alert config
  const saveAlertConfig = async () => {
    if (!alertConfig) return
    try {
      const res = await fetch(`${API}/api/risk/alerts/configure`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(alertConfig),
      })
      if (res.ok) {
        setAlertConfig(await res.json())
        setTestEmailStatus('Config saved!')
        setTimeout(() => setTestEmailStatus(''), 3000)
      }
    } catch {
      setTestEmailStatus('Failed to save')
    }
  }

  // Send test email
  const sendTestEmail = async () => {
    setTestEmailStatus('Sending...')
    try {
      const res = await fetch(`${API}/api/risk/alerts/test-email`, { method: 'POST' })
      const data = await res.json()
      if (data.status === 'sent') {
        setTestEmailStatus(`Sent via ${data.auth_method === 'oauth2' ? 'OAuth2' : 'App Password'} to ${data.recipient}`)
      } else {
        setTestEmailStatus(data.message || 'Failed')
      }
    } catch {
      setTestEmailStatus('Failed to send')
    }
    setTimeout(() => setTestEmailStatus(''), 8000)
  }

  // ── Render ─────────────────────────────────────────────────────

  if (loading) {
    return (
      <div className="flex items-center justify-center h-96">
        <div className="text-center">
          <div className="w-8 h-8 border-2 border-accent border-t-transparent rounded-full animate-spin mx-auto mb-3" />
          <p className="text-gray-400">Loading risk data...</p>
        </div>
      </div>
    )
  }

  if (error && !summary) {
    return (
      <div className="flex items-center justify-center h-96">
        <p className="text-danger">{error}</p>
      </div>
    )
  }

  const s = summary

  // Risk metric cards
  const riskMetrics = s
    ? [
        {
          title: 'Value at Risk (95%)',
          value: `$${fmt(s.var_95)}`,
          subtitle: '1-day VaR from equity curve',
          color: N(s.var_95) > N(s.equity) * 0.03 ? 'text-danger' : 'text-warning',
        },
        {
          title: 'CVaR (95%)',
          value: `$${fmt(s.cvar_95)}`,
          subtitle: 'Expected Shortfall',
          color: N(s.cvar_95) > N(s.equity) * 0.05 ? 'text-danger' : 'text-warning',
        },
        {
          title: 'Current Drawdown',
          value: `${N(s.current_dd) > 0 ? '-' : ''}${N(s.current_dd).toFixed(2)}%`,
          subtitle: 'From peak equity',
          color:
            N(s.current_dd) > 10 ? 'text-danger' : N(s.current_dd) > 5 ? 'text-warning' : 'text-success',
        },
        {
          title: 'Max Drawdown',
          value: `-${N(s.max_dd).toFixed(2)}%`,
          subtitle: `Over ${(s.equity_curve || []).length} trading days`,
          color: N(s.max_dd) > 15 ? 'text-danger' : N(s.max_dd) > 10 ? 'text-warning' : 'text-success',
        },
        {
          title: 'Gross Leverage',
          value: `${N(s.gross_leverage).toFixed(2)}x`,
          subtitle: s.position_open ? s.position_detail : 'No open position',
          color: N(s.gross_leverage) > 3 ? 'text-danger' : 'text-gray-100',
        },
        {
          title: 'Net Exposure',
          value: s.position_open ? (s.net_exposure || '0') : '0',
          subtitle: s.position_open ? (s.position_detail || 'Open') : 'Flat (no position)',
          color: 'text-accent',
        },
      ]
    : []

  // Greeks (only for options strategies)
  const greeks = s?.has_greeks
    ? [
        { name: 'Delta', value: N(s.portfolio_delta), range: [-500, 500] as [number, number], unit: '', color: '#3b82f6' },
        { name: 'Gamma', value: N(s.portfolio_gamma), range: [0, 100] as [number, number], unit: '', color: '#10b981' },
        { name: 'Theta', value: N(s.portfolio_theta), range: [-5000, 5000] as [number, number], unit: '$/day', color: '#ef4444' },
        { name: 'Vega', value: N(s.portfolio_vega), range: [-1000, 1000] as [number, number], unit: '$/1%', color: '#a855f7' },
      ]
    : []

  return (
    <div className="space-y-6">
      {/* ── Header ── */}
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Risk Dashboard</h2>
        <div className="flex items-center gap-3">
          <button
            onClick={() => setShowAlertPanel(!showAlertPanel)}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              showAlertPanel
                ? 'bg-accent text-white'
                : 'bg-surface border border-border text-gray-400 hover:text-gray-100'
            }`}
          >
            Alert Settings
          </button>
          {s && (
            <span
              className={`inline-flex items-center gap-1.5 text-xs px-2 py-1 rounded-full ${
                s.status === 'running'
                  ? 'bg-success/20 text-success'
                  : 'bg-gray-700 text-gray-400'
              }`}
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  s.status === 'running' ? 'bg-success' : 'bg-gray-500'
                }`}
              />
              {s.status === 'running' ? 'Live' : s.status === 'no_data' ? 'No Data' : 'Offline'}
            </span>
          )}
        </div>
      </div>

      {/* ── Portfolio Budgets (cross-strategy) ── */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div>
            <h3 className="text-lg font-semibold">Portfolio Budgets</h3>
            <p className="text-xs text-gray-500">
              Cross-strategy vega &amp; tail budgets — all paper books combined (advisory v1)
            </p>
          </div>
          {portfolio && !portfolio.error && (
            <div className="flex items-center gap-4">
              {Object.entries(portfolio.flags || {}).map(([sid, flag]) => (
                <span
                  key={sid}
                  className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/40"
                >
                  <span className="w-1.5 h-1.5 rounded-full bg-rose-400 animate-pulse" />
                  {sid}: {flag}
                </span>
              ))}
              <div className="text-right">
                <span className="text-xs text-gray-500">Combined Equity</span>
                <p className="text-lg font-bold text-success">
                  ${fmt(portfolio.combined_equity_usd)}
                </p>
              </div>
            </div>
          )}
        </div>

        {portfolioError || (portfolio && portfolio.error) ? (
          <div className="bg-background rounded-lg p-6 text-center">
            <p className="text-sm text-gray-500">Portfolio budgets unavailable</p>
            <p className="text-xs text-gray-600 mt-1">
              {portfolio?.error || 'Backend unreachable — retrying every 60s'}
            </p>
          </div>
        ) : !portfolio ? (
          <div className="bg-background rounded-lg p-6 text-center">
            <p className="text-sm text-gray-500">Loading portfolio exposures…</p>
          </div>
        ) : (
          <>
            {/* Budget utilization bars (vega + notional) */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4">
              {(portfolio.utilization || [])
                .filter((u) => !u.id.startsWith('stress:'))
                .map((u) => {
                  const pct = Math.min(N(u.utilization) * 100, 100)
                  const barColor = u.breached
                    ? 'bg-rose-500'
                    : pct > 80
                    ? 'bg-amber-400'
                    : 'bg-success'
                  const usedStr =
                    u.id === 'abs_vega'
                      ? `$${fmt(u.used)}`
                      : `${(N(u.used) * 100).toFixed(1)}%`
                  const limitStr =
                    u.id === 'abs_vega'
                      ? `$${fmt(u.limit)} / vol-pt`
                      : `${(N(u.limit) * 100).toFixed(0)}% of equity`
                  return (
                    <div key={u.id} className="bg-background rounded-lg p-4">
                      <div className="flex items-center justify-between mb-2">
                        <span className="text-sm text-gray-100">{u.label}</span>
                        <span
                          className={`text-xs font-semibold ${
                            u.breached
                              ? 'text-rose-400'
                              : pct > 80
                              ? 'text-amber-400'
                              : 'text-gray-400'
                          }`}
                        >
                          {usedStr} <span className="text-gray-600">/ {limitStr}</span>
                        </span>
                      </div>
                      <div className="h-2 bg-surface rounded-full overflow-hidden border border-border">
                        <div
                          className={`h-full rounded-full transition-all ${barColor}`}
                          style={{ width: `${Math.max(pct, 1)}%` }}
                        />
                      </div>
                      <p className="text-[11px] text-gray-500 mt-1">
                        {(N(u.utilization) * 100).toFixed(1)}% of budget used
                        {u.breached && <span className="text-rose-400 ml-1">— BREACHED</span>}
                      </p>
                    </div>
                  )
                })}
            </div>

            {/* Stress scenario cards */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {Object.entries(portfolio.stress || {}).map(([name, sc]) => {
                const budgetPct = N(portfolio.budgets?.max_stress_loss_pct) * 100
                const lossPct = N(sc.loss_pct) * 100
                const util = budgetPct > 0 ? (lossPct / budgetPct) * 100 : 0
                const breached = portfolio.breaches.includes(`stress:${name}`)
                return (
                  <div
                    key={name}
                    className={`bg-background rounded-lg p-4 border ${
                      breached
                        ? 'border-rose-500/50'
                        : util > 80
                        ? 'border-amber-400/40'
                        : 'border-border'
                    }`}
                  >
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-semibold text-gray-100">{name}</span>
                      <span
                        className={`text-sm font-bold ${
                          breached ? 'text-rose-400' : util > 80 ? 'text-amber-400' : 'text-gray-100'
                        }`}
                      >
                        -${fmt(sc.loss_usd)}
                      </span>
                    </div>
                    <p className="text-[11px] text-gray-500 mb-2">{sc.description}</p>
                    <div className="h-1.5 bg-surface rounded-full overflow-hidden border border-border">
                      <div
                        className={`h-full rounded-full transition-all ${
                          breached ? 'bg-rose-500' : util > 80 ? 'bg-amber-400' : 'bg-success'
                        }`}
                        style={{ width: `${Math.min(Math.max(util, 1), 100)}%` }}
                      />
                    </div>
                    <p className="text-[11px] text-gray-500 mt-1">
                      {lossPct.toFixed(1)}% of equity <span className="text-gray-600">/ {budgetPct.toFixed(0)}% budget</span>
                      {breached && <span className="text-rose-400 ml-1">— BREACHED</span>}
                    </p>
                  </div>
                )
              })}
            </div>

            {/* Degraded books + timestamp */}
            <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
              <div className="flex items-center gap-2">
                {(portfolio.degraded || []).map((sid) => (
                  <span
                    key={sid}
                    className="text-[11px] px-2 py-0.5 rounded bg-amber-500/15 text-amber-400"
                  >
                    {sid}: data degraded
                  </span>
                ))}
              </div>
              <span className="text-[11px] text-gray-600">
                Net vega ${fmt(portfolio.exposures?.net_vega_usd)}/vol-pt · updated{' '}
                {portfolio.generated_at} · refreshes every 60s
              </span>
            </div>
          </>
        )}
      </div>

      {/* ── Strategy Selector ── */}
      <div className="bg-surface rounded-xl border border-border p-4">
        <div className="flex items-center gap-3 mb-3">
          <span className="text-sm text-gray-400">Select Strategy:</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3">
          {strategies.map((strat) => (
            <button
              key={strat.strategy_id}
              onClick={() => {
                setSelected(strat.strategy_id)
                setLoading(true)
              }}
              className={`p-3 rounded-lg border text-left transition-all ${
                selected === strat.strategy_id
                  ? 'border-accent bg-accent/10'
                  : 'border-border bg-background hover:border-gray-600'
              }`}
            >
              <div className="flex items-center justify-between mb-1">
                <span className="text-sm font-semibold text-gray-100 truncate">{strat.name}</span>
                <span
                  className={`w-2 h-2 rounded-full flex-shrink-0 ml-2 ${
                    strat.status === 'running' ? 'bg-success' : 'bg-gray-600'
                  }`}
                />
              </div>
              <span
                className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium ${
                  ASSET_BADGE[strat.asset_class] || 'bg-gray-500/20 text-gray-400'
                }`}
              >
                {strat.asset_class}
              </span>
              {strat.status === 'running' ? (
                <div className="mt-2 grid grid-cols-2 gap-1 text-[11px]">
                  <div>
                    <span className="text-gray-500">P&L</span>
                    <p
                      className={`font-semibold ${
                        strat.pnl >= 0 ? 'text-success' : 'text-danger'
                      }`}
                    >
                      {strat.pnl >= 0 ? '+' : ''}${fmt(strat.pnl)}
                    </p>
                  </div>
                  <div>
                    <span className="text-gray-500">MaxDD</span>
                    <p
                      className={`font-semibold ${
                        strat.max_dd > 10 ? 'text-danger' : strat.max_dd > 5 ? 'text-warning' : 'text-gray-300'
                      }`}
                    >
                      -{N(strat.max_dd).toFixed(1)}%
                    </p>
                  </div>
                </div>
              ) : (
                <p className="mt-2 text-[11px] text-gray-600">No live data</p>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* ── Alert Settings Panel ── */}
      {showAlertPanel && alertConfig && (
        <div className="bg-surface rounded-xl border border-accent/30 p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold">Email Alert Configuration</h3>
            <div className="flex items-center gap-3">
              {testEmailStatus && (
                <span className="text-xs text-accent">{testEmailStatus}</span>
              )}
              <button
                onClick={sendTestEmail}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-surface border border-border text-gray-400 hover:text-gray-100 transition-colors"
              >
                Send Test Email
              </button>
              <button
                onClick={saveAlertConfig}
                className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent text-white hover:bg-accent/80 transition-colors"
              >
                Save
              </button>
            </div>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
            {/* Email toggle */}
            <div className="col-span-full flex flex-wrap items-center gap-3">
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={alertConfig.email_enabled}
                  onChange={(e) =>
                    setAlertConfig({ ...alertConfig, email_enabled: e.target.checked })
                  }
                  className="sr-only peer"
                />
                <div className="w-9 h-5 bg-gray-700 rounded-full peer peer-checked:bg-accent transition-colors after:content-[''] after:absolute after:top-0.5 after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-full" />
              </label>
              <span className="text-sm text-gray-300">Email alerts enabled</span>
              <span className="text-xs text-gray-500 ml-2">Recipient: {alertConfig.recipient}</span>
            </div>
            <div className="col-span-full bg-background rounded-lg p-3 text-xs text-gray-400 space-y-1">
              <p className="text-gray-300 font-medium">Setup (choose one):</p>
              <p><span className="text-success font-mono">OAuth2 (recommended):</span> python -m dashboard.alerts.email_notifier --setup</p>
              <p><span className="text-warning font-mono">App Password (fallback):</span> Set ALERT_EMAIL_SENDER + ALERT_EMAIL_PASSWORD in .env</p>
              <p className="text-gray-500">OAuth2 only needs send permission, never stores your password.</p>
            </div>

            <div>
              <label className="block text-xs text-gray-500 mb-1">DD Warning (%)</label>
              <input
                type="number"
                value={alertConfig.drawdown_warning_pct}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, drawdown_warning_pct: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">DD Critical (%)</label>
              <input
                type="number"
                value={alertConfig.drawdown_critical_pct}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, drawdown_critical_pct: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Daily Loss Warning (%)</label>
              <input
                type="number"
                value={alertConfig.daily_loss_warning_pct}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, daily_loss_warning_pct: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Daily Loss Critical (%)</label>
              <input
                type="number"
                value={alertConfig.daily_loss_critical_pct}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, daily_loss_critical_pct: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">VIX Spike Threshold</label>
              <input
                type="number"
                value={alertConfig.vix_spike_threshold}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, vix_spike_threshold: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Consecutive Losses</label>
              <input
                type="number"
                value={alertConfig.consecutive_loss_threshold}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, consecutive_loss_threshold: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Leverage Warning (x)</label>
              <input
                type="number"
                value={alertConfig.leverage_warning}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, leverage_warning: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-500 mb-1">Leverage Critical (x)</label>
              <input
                type="number"
                value={alertConfig.leverage_critical}
                onChange={(e) =>
                  setAlertConfig({ ...alertConfig, leverage_critical: +e.target.value })
                }
                className="w-full px-3 py-1.5 rounded-lg bg-background border border-border text-sm text-gray-100"
              />
            </div>
          </div>
        </div>
      )}

      {/* ── Strategy Summary Bar ── */}
      {s && s.status !== 'no_data' && (
        <div className="bg-surface rounded-xl border border-border p-4 flex flex-wrap items-center gap-6">
          <div>
            <span className="text-xs text-gray-500">Strategy</span>
            <p className="text-sm font-semibold">{s.name}</p>
          </div>
          <div>
            <span className="text-xs text-gray-500">Equity</span>
            <p className="text-sm font-semibold text-success">${fmt(s.equity)}</p>
          </div>
          <div>
            <span className="text-xs text-gray-500">Return</span>
            <p
              className={`text-sm font-semibold ${
                s.return_pct >= 0 ? 'text-success' : 'text-danger'
              }`}
            >
              {s.return_pct >= 0 ? '+' : ''}
              {N(s.return_pct).toFixed(2)}%
            </p>
          </div>
          <div>
            <span className="text-xs text-gray-500">P&L</span>
            <p
              className={`text-sm font-semibold ${
                s.total_pnl >= 0 ? 'text-success' : 'text-danger'
              }`}
            >
              {s.total_pnl >= 0 ? '+' : ''}${fmt(s.total_pnl)}
            </p>
          </div>
          <div>
            <span className="text-xs text-gray-500">Trades</span>
            <p className="text-sm font-semibold">{s.total_trades}</p>
          </div>
          <div>
            <span className="text-xs text-gray-500">Position</span>
            <p className="text-sm font-semibold">{s.position_detail}</p>
          </div>
        </div>
      )}

      {/* ── No Data State ── */}
      {s && s.status === 'no_data' && (
        <div className="bg-surface rounded-xl border border-border p-12 text-center">
          <p className="text-gray-500 text-lg mb-2">No live data for {s.name}</p>
          <p className="text-gray-600 text-sm">
            Deploy and run this strategy to see live risk metrics here.
          </p>
        </div>
      )}

      {/* ── Risk Metric Cards ── */}
      {s && s.status !== 'no_data' && (
        <>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {riskMetrics.map((metric) => (
              <div key={metric.title} className="bg-surface rounded-xl border border-border p-4">
                <p className="text-sm text-gray-400">{metric.title}</p>
                <p className={`text-2xl font-bold mt-1 ${metric.color}`}>{metric.value}</p>
                <p className="text-xs text-gray-500 mt-1">{metric.subtitle}</p>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* ── Greeks (options only) / Position Info (others) ── */}
            <div className="bg-surface rounded-xl border border-border p-6">
              {s.has_greeks ? (
                <>
                  <h3 className="text-lg font-semibold mb-1">Greeks Exposure</h3>
                  <p className="text-xs text-gray-500 mb-4">
                    {s.position_open ? s.position_detail : 'No active position'}
                  </p>
                  <div className="space-y-5">
                    {greeks.map((greek) => {
                      const rangeSpan = greek.range[1] - greek.range[0]
                      const normalizedPct =
                        ((greek.value - greek.range[0]) / rangeSpan) * 100
                      const clampedPct = Math.max(0, Math.min(100, normalizedPct))
                      return (
                        <div key={greek.name}>
                          <div className="flex items-center justify-between mb-1.5">
                            <span className="text-sm font-medium text-gray-100">
                              {greek.name}
                            </span>
                            <span className="text-sm text-gray-400">
                              {greek.value.toLocaleString(undefined, {
                                minimumFractionDigits: 2,
                              })}
                              {greek.unit && (
                                <span className="text-xs text-gray-500 ml-1">{greek.unit}</span>
                              )}
                            </span>
                          </div>
                          <div className="relative h-3 bg-background rounded-full overflow-hidden">
                            <div
                              className="absolute top-0 left-0 h-full rounded-full transition-all"
                              style={{
                                width: `${clampedPct}%`,
                                backgroundColor: greek.color,
                              }}
                            />
                            {greek.range[0] < 0 && (
                              <div
                                className="absolute top-0 h-full w-0.5 bg-gray-500"
                                style={{
                                  left: `${((0 - greek.range[0]) / rangeSpan) * 100}%`,
                                }}
                              />
                            )}
                          </div>
                          <div className="flex justify-between mt-0.5">
                            <span className="text-[10px] text-gray-600">{greek.range[0]}</span>
                            <span className="text-[10px] text-gray-600">{greek.range[1]}</span>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </>
              ) : (
                <>
                  <h3 className="text-lg font-semibold mb-1">Position & Exposure</h3>
                  <p className="text-xs text-gray-500 mb-4">{s.asset_class} strategy</p>
                  <div className="space-y-4">
                    <div className="flex items-center justify-between p-3 bg-background rounded-lg">
                      <span className="text-sm text-gray-400">Position</span>
                      <span className={`text-sm font-semibold ${s.position_open ? 'text-accent' : 'text-gray-500'}`}>
                        {s.position_detail}
                      </span>
                    </div>
                    <div className="flex items-center justify-between p-3 bg-background rounded-lg">
                      <span className="text-sm text-gray-400">Leverage</span>
                      <span className={`text-sm font-semibold ${s.gross_leverage > 3 ? 'text-danger' : 'text-gray-100'}`}>
                        {N(s.gross_leverage).toFixed(2)}x
                      </span>
                    </div>
                    <div className="flex items-center justify-between p-3 bg-background rounded-lg">
                      <span className="text-sm text-gray-400">Exposure</span>
                      <span className="text-sm font-semibold text-gray-100">{s.net_exposure}</span>
                    </div>
                    <div className="flex items-center justify-between p-3 bg-background rounded-lg">
                      <span className="text-sm text-gray-400">Consecutive Losses</span>
                      <span className={`text-sm font-semibold ${s.consecutive_losses > 3 ? 'text-danger' : 'text-gray-100'}`}>
                        {s.consecutive_losses}
                      </span>
                    </div>
                    <div className="flex items-center justify-between p-3 bg-background rounded-lg">
                      <span className="text-sm text-gray-400">Total Trades</span>
                      <span className="text-sm font-semibold text-gray-100">{s.total_trades}</span>
                    </div>
                  </div>
                </>
              )}
            </div>

            {/* ── Circuit Breakers ── */}
            <div className="bg-surface rounded-xl border border-border p-6">
              <h3 className="text-lg font-semibold mb-4">Circuit Breakers</h3>
              <div className="space-y-4">
                {(s.circuit_breakers || []).map((cb) => (
                  <div key={cb.name} className="flex items-center gap-3">
                    <div
                      className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${
                        cb.status === 'ok'
                          ? 'bg-success'
                          : cb.status === 'warning'
                          ? 'bg-warning animate-pulse'
                          : 'bg-danger animate-pulse'
                      }`}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-sm text-gray-100">{cb.name}</span>
                        <span className="text-xs text-gray-400">
                          {cb.current}{' '}
                          <span className="text-gray-600">/ {cb.threshold}</span>
                        </span>
                      </div>
                      <div className="h-1.5 bg-background rounded-full overflow-hidden">
                        <div
                          className={`h-full rounded-full transition-all ${
                            cb.pct < 50
                              ? 'bg-success'
                              : cb.pct < 75
                              ? 'bg-warning'
                              : 'bg-danger'
                          }`}
                          style={{ width: `${cb.pct}%` }}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
              <div className="mt-4 pt-4 border-t border-border">
                <div className="flex items-center gap-2">
                  <div
                    className={`w-2.5 h-2.5 rounded-full ${
                      (s.circuit_breakers || []).every((cb) => cb.status === 'ok')
                        ? 'bg-success'
                        : 'bg-warning animate-pulse'
                    }`}
                  />
                  <span className="text-xs text-gray-400">
                    {(s.circuit_breakers || []).every((cb) => cb.status === 'ok')
                      ? 'All circuit breakers operational'
                      : 'Warning: circuit breaker threshold approaching'}
                  </span>
                </div>
              </div>
            </div>
          </div>

          {/* ── Drawdown Timeline ── */}
          <div className="bg-surface rounded-xl border border-border p-6">
            <h3 className="text-lg font-semibold mb-4">Drawdown Timeline</h3>
            {(s.drawdown_timeline || []).length > 0 ? (
              <div className="space-y-1">
                {s.drawdown_timeline.map((point, i) => {
                  const maxW = Math.max(
                    ...s.drawdown_timeline.map((p) => Math.abs(p.dd)),
                    1
                  )
                  const barW = (Math.abs(point.dd) / maxW) * 100
                  return (
                    <div key={i} className="flex items-center gap-3 text-xs">
                      <span className="text-gray-500 w-20 flex-shrink-0">{point.date}</span>
                      <div className="flex-1 h-4 bg-background rounded overflow-hidden relative">
                        <div
                          className={`h-full rounded ${
                            point.dd < -5
                              ? 'bg-danger/60'
                              : point.dd < -2
                              ? 'bg-warning/50'
                              : 'bg-success/30'
                          }`}
                          style={{ width: `${Math.max(barW, 1)}%` }}
                        />
                      </div>
                      <span
                        className={`w-14 text-right ${
                          point.dd < -5
                            ? 'text-danger'
                            : point.dd < -2
                            ? 'text-warning'
                            : 'text-gray-400'
                        }`}
                      >
                        {N(point.dd).toFixed(2)}%
                      </span>
                    </div>
                  )
                })}
              </div>
            ) : (
              <p className="text-sm text-gray-500">No drawdown data yet</p>
            )}
          </div>

          {/* ── Market Regime (shown for strategies with market data) ── */}
          {(s.spy_price > 0 || s.vix > 0) && (
            <div className="bg-surface rounded-xl border border-border p-6">
              <h3 className="text-lg font-semibold mb-4">Market Regime</h3>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                {s.spy_price > 0 && (
                  <div>
                    <p className="text-xs text-gray-500">SPY Price</p>
                    <p className="text-lg font-bold">${N(s.spy_price).toFixed(2)}</p>
                  </div>
                )}
                {s.vix > 0 && (
                  <div>
                    <p className="text-xs text-gray-500">VIX</p>
                    <p
                      className={`text-lg font-bold ${
                        s.vix > 30
                          ? 'text-danger'
                          : s.vix > 20
                          ? 'text-warning'
                          : 'text-success'
                      }`}
                    >
                      {N(s.vix).toFixed(2)}
                    </p>
                  </div>
                )}
                {s.implied_vol > 0 && (
                  <div>
                    <p className="text-xs text-gray-500">Implied Vol</p>
                    <p className="text-lg font-bold">{(N(s.implied_vol) * 100).toFixed(1)}%</p>
                  </div>
                )}
                {s.rv_20d > 0 && (
                  <div>
                    <p className="text-xs text-gray-500">Realized Vol (20d)</p>
                    <p className="text-lg font-bold">{(N(s.rv_20d) * 100).toFixed(1)}%</p>
                  </div>
                )}
                <div>
                  <p className="text-xs text-gray-500">VRP (IV - RV)</p>
                  <p
                    className={`text-lg font-bold ${
                      s.vrp > 0.05
                        ? 'text-success'
                        : s.vrp > 0
                        ? 'text-gray-100'
                        : 'text-danger'
                    }`}
                  >
                    {s.vrp > 0 ? '+' : ''}
                    {(N(s.vrp) * 100).toFixed(1)}%
                  </p>
                </div>
                <div>
                  <p className="text-xs text-gray-500">Vol Regime</p>
                  <p
                    className={`text-lg font-bold ${
                      s.vix > 30
                        ? 'text-danger'
                        : s.vix > 25
                        ? 'text-warning'
                        : s.vix > 18
                        ? 'text-gray-100'
                        : 'text-success'
                    }`}
                  >
                    {s.vix > 30
                      ? 'High Vol'
                      : s.vix > 25
                      ? 'Elevated'
                      : s.vix > 18
                      ? 'Normal'
                      : 'Low Vol'}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-gray-500">VRP Signal</p>
                  <p
                    className={`text-lg font-bold ${
                      s.vrp > 0.08
                        ? 'text-success'
                        : s.vrp > 0.03
                        ? 'text-gray-100'
                        : 'text-warning'
                    }`}
                  >
                    {s.vrp > 0.08
                      ? 'Strong Sell Vol'
                      : s.vrp > 0.03
                      ? 'Sell Vol'
                      : s.vrp > 0
                      ? 'Neutral'
                      : 'Buy Vol'}
                  </p>
                </div>
                <div>
                  <p className="text-xs text-gray-500">Strategy Direction</p>
                  <p className="text-lg font-bold">
                    {s.position_open
                      ? s.position_detail.toUpperCase()
                      : 'WAITING'}
                  </p>
                </div>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}
