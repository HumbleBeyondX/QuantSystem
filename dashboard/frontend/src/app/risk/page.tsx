'use client'

import { Fragment, useCallback, useEffect, useState } from 'react'

/**
 * Risk Dashboard Page — universal cross-strategy book view
 *
 * Built around the UNIVERSAL book contract from core/risk/portfolio.py:
 * every book (options selling, trend scalp, market making, funding
 * carry, future HFT) reports equity / day P&L / gross-net notional /
 * leverage / rails / freshness. Type-specific facts (greeks, legs,
 * inventory, funding) are demoted to per-book expandable detail.
 *
 * Layout:
 *  1. Combined header — equity, day P&L, gross leverage vs budget,
 *     combined drawdown sparkline
 *  2. Books table — one universal row per book, click to expand detail
 *  3. Stress matrix — scenarios x books ($ losses, red by % of equity)
 *  4. Budget utilization bars + REDUCE_ONLY flags
 *  5. Email alert configuration (bottom)
 *
 * Auto-refresh every 60s. Degraded books render grey with their error.
 */

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8000'

// ── Types ────────────────────────────────────────────────────────

interface Rails {
  daily_loss_cap_pct: number | null
  daily_used_pct: number | null
  halted: boolean | null
}

interface Freshness {
  last_telemetry: string | null
  age_hours: number | null
  fresh: boolean
}

interface OptionsLeg {
  instrument: string
  side: string
  strike: number
  mark_price_usd: number
  mark_iv: number
}

interface ScalpPosition {
  dir: number
  entry: number
  qty: number
  stop?: number
  notional_usd?: number
}

interface MmInventory {
  inv_shares: number | null
  inv_usd: number
  fills: number | null
  captured_usd: number | null
}

interface CarryPair {
  short_base?: string
  long_base?: string
  s_entry?: number
  l_entry?: number
  s_qty?: number
  l_qty?: number
  opened?: string
  funding_pnl?: number
}

/** Type-specific detail — all fields optional, keyed by book_type. */
interface BookDetail {
  // options
  greeks?: Record<string, number>
  legs?: OptionsLeg[]
  direction?: string
  n_contracts?: number
  strike?: number
  spot?: number
  implied_vol?: number
  index_usd?: number
  mult?: number
  credit_total_usd?: number
  // linear
  positions?: Record<string, ScalpPosition>
  n_trades?: number
  wins?: number
  // mm
  inventory?: Record<string, MmInventory>
  cash_usd?: number
  // carry
  pair?: CarryPair | null
  funding_collected_usd?: number
  n_rounds?: number
}

interface BookExposure {
  strategy_id: string
  book_type: 'options' | 'linear' | 'mm' | 'carry'
  asset: string
  equity_usd: number
  day_pnl_usd: number
  gross_notional_usd: number
  net_notional_usd: number
  leverage: number
  position_open: boolean
  vega_usd: number
  delta_usd: number
  short_vol_notional_usd: number
  rails: Rails
  freshness: Freshness
  detail: BookDetail
  error: string | null
}

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

interface CurvePoint {
  date: string
  equity: number
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
    combined_day_pnl_usd: number
    combined_gross_notional_usd: number
    combined_net_notional_usd: number
    gross_leverage: number
    net_vega_usd: number
    abs_vega_usd: number
    short_vol_notional_usd: number
    short_vol_notional_pct: number
    degraded: string[]
  }
  stress: Record<string, StressResult>
  equity_curves: Record<string, CurvePoint[]>
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

const fmt0 = (n: number | null | undefined) =>
  (n ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })

/** Safe number: coerce null/undefined to 0 */
const N = (v: number | null | undefined): number => v ?? 0

const signCls = (v: number) => (v > 0 ? 'text-success' : v < 0 ? 'text-danger' : 'text-gray-400')

const signStr = (v: number) => `${v >= 0 ? '+' : '-'}$${fmt(Math.abs(v))}`

const ageStr = (h: number | null) => {
  if (h == null) return 'n/a'
  if (h < 1) return `${Math.round(h * 60)}m`
  if (h < 48) return `${h.toFixed(0)}h`
  return `${(h / 24).toFixed(0)}d`
}

const BOOK_TYPE_BADGE: Record<string, string> = {
  options: 'bg-amber-500/20 text-amber-400',
  linear: 'bg-blue-500/20 text-blue-400',
  mm: 'bg-purple-500/20 text-purple-400',
  carry: 'bg-teal-500/20 text-teal-400',
}

const ASSET_BADGE: Record<string, string> = {
  spy: 'bg-sky-500/15 text-sky-400',
  btc: 'bg-orange-500/15 text-orange-400',
  tokenized: 'bg-fuchsia-500/15 text-fuchsia-400',
}

// ── Sparkline (combined drawdown, inline SVG) ────────────────────

function DrawdownSparkline({ curve }: { curve: CurvePoint[] }) {
  if (!curve || curve.length < 2) {
    return <p className="text-[11px] text-gray-600">Not enough history</p>
  }
  const W = 200
  const H = 44
  let peak = -Infinity
  const dd = curve.map((p) => {
    peak = Math.max(peak, p.equity)
    return peak > 0 ? (p.equity / peak - 1) * 100 : 0
  })
  const minDd = Math.min(...dd, -0.1) // at least 0.1% span so flat curves render
  const pts = dd
    .map((v, i) => {
      const x = (i / (dd.length - 1)) * W
      const y = (v / minDd) * (H - 4) + 2 // 0% dd at top, worst dd at bottom
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const last = dd[dd.length - 1]
  return (
    <div>
      <svg width={W} height={H} className="block">
        <polyline
          points={pts}
          fill="none"
          stroke={last < -5 ? '#f43f5e' : last < -2 ? '#fbbf24' : '#22c55e'}
          strokeWidth="1.5"
        />
        <line x1="0" y1="2" x2={W} y2="2" stroke="#374151" strokeWidth="0.5" strokeDasharray="3 3" />
      </svg>
      <p className="text-[11px] text-gray-500 mt-0.5">
        Combined drawdown {last.toFixed(2)}% · worst {minDd.toFixed(2)}% · {curve.length}d
      </p>
    </div>
  )
}

// ── Per-book expandable detail (type-specific, demoted greeks) ───

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between px-3 py-1.5 bg-background rounded">
      <span className="text-xs text-gray-500">{label}</span>
      <span className="text-xs font-mono text-gray-200">{value}</span>
    </div>
  )
}

function BookDetailPanel({ book }: { book: BookExposure }) {
  const d = book.detail || {}
  if (book.error) {
    return <p className="text-xs text-amber-400 px-3 py-2">Degraded: {book.error}</p>
  }
  switch (book.book_type) {
    case 'options': {
      const greeks = d.greeks || {}
      return (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 p-3">
          <div className="space-y-1.5">
            <p className="text-[11px] uppercase tracking-wide text-gray-500 font-semibold">Greeks</p>
            <DetailRow label="Vega ($/vol-pt)" value={fmt(N(greeks.vega_usd ?? book.vega_usd))} />
            <DetailRow label="Delta ($/1% spot)" value={fmt(book.delta_usd)} />
            {greeks.portfolio_delta_shares !== undefined && (
              <DetailRow label="Delta (shares)" value={fmt(greeks.portfolio_delta_shares)} />
            )}
            {greeks.portfolio_gamma !== undefined && (
              <DetailRow label="Gamma" value={fmt(greeks.portfolio_gamma)} />
            )}
            {greeks.portfolio_theta_usd_day !== undefined && (
              <DetailRow label="Theta ($/day)" value={fmt(greeks.portfolio_theta_usd_day)} />
            )}
            <DetailRow label="Short-vol notional" value={`$${fmt(book.short_vol_notional_usd)}`} />
          </div>
          <div className="space-y-1.5">
            <p className="text-[11px] uppercase tracking-wide text-gray-500 font-semibold">
              {book.position_open ? 'Legs' : 'Position'}
            </p>
            {(d.legs || []).map((leg) => (
              <DetailRow
                key={leg.instrument}
                label={`${leg.side} ${leg.instrument}`}
                value={`K ${fmt0(leg.strike)} · mark $${fmt(leg.mark_price_usd)} · IV ${leg.mark_iv.toFixed(1)}`}
              />
            ))}
            {!book.position_open && (
              <DetailRow label="Status" value={`Flat (${d.direction || 'none'})`} />
            )}
            {d.n_contracts !== undefined && book.position_open && (
              <DetailRow
                label="Straddle"
                value={`${d.direction} ${d.n_contracts}c @ $${fmt0(d.strike)} · spot $${fmt(d.spot)}`}
              />
            )}
            {d.credit_total_usd != null && (
              <DetailRow label="Credit collected" value={`$${fmt(d.credit_total_usd)}`} />
            )}
          </div>
        </div>
      )
    }
    case 'linear': {
      const positions = Object.entries(d.positions || {})
      return (
        <div className="p-3 space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-gray-500 font-semibold">
            Open positions ({positions.length})
          </p>
          {positions.length === 0 && <DetailRow label="Status" value="Flat" />}
          {positions.map(([sym, p]) => (
            <DetailRow
              key={sym}
              label={`${p.dir > 0 ? 'LONG' : 'SHORT'} ${sym}`}
              value={`${N(p.qty).toFixed(2)} @ $${fmt(p.entry)} · $${fmt(p.notional_usd)} notional${
                p.stop != null ? ` · stop $${fmt(p.stop)}` : ''
              }`}
            />
          ))}
          <DetailRow label="Trades (wins)" value={`${N(d.n_trades)} (${N(d.wins)})`} />
        </div>
      )
    }
    case 'mm': {
      const inv = Object.entries(d.inventory || {})
      return (
        <div className="p-3 space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-gray-500 font-semibold">
            Inventory per symbol
          </p>
          {inv.map(([sym, b]) => (
            <DetailRow
              key={sym}
              label={sym}
              value={`$${fmt(b.inv_usd)} (${N(b.inv_shares).toFixed(2)} sh) · ${N(b.fills)} fills · captured $${fmt(
                b.captured_usd
              )}`}
            />
          ))}
          <DetailRow label="Cash" value={`$${fmt(d.cash_usd)}`} />
        </div>
      )
    }
    case 'carry': {
      const pair = d.pair
      return (
        <div className="p-3 space-y-1.5">
          <p className="text-[11px] uppercase tracking-wide text-gray-500 font-semibold">
            Carry pair
          </p>
          {pair ? (
            <>
              <DetailRow
                label={`SHORT ${pair.short_base ?? '?'}`}
                value={`${N(pair.s_qty).toFixed(2)} @ $${fmt(pair.s_entry)}`}
              />
              <DetailRow
                label={`LONG ${pair.long_base ?? '?'}`}
                value={`${N(pair.l_qty).toFixed(2)} @ $${fmt(pair.l_entry)}`}
              />
              {pair.opened && <DetailRow label="Opened" value={pair.opened} />}
            </>
          ) : (
            <DetailRow label="Status" value="No pair open (scanning funding)" />
          )}
          <DetailRow label="Funding collected" value={`$${fmt(d.funding_collected_usd)}`} />
          <DetailRow label="Rounds" value={`${N(d.n_rounds)}`} />
        </div>
      )
    }
  }
}

// ── Component ────────────────────────────────────────────────────

export default function RiskPage() {
  const [portfolio, setPortfolio] = useState<PortfolioRisk | null>(null)
  const [portfolioError, setPortfolioError] = useState(false)
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [alertConfig, setAlertConfig] = useState<AlertConfig | null>(null)
  const [showAlertPanel, setShowAlertPanel] = useState(false)
  const [testEmailStatus, setTestEmailStatus] = useState('')

  // Fetch cross-strategy portfolio report (slower: hits Deribit)
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
    } finally {
      setLoading(false)
    }
  }, [])

  const fetchAlertConfig = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/risk/alerts/config`)
      if (res.ok) setAlertConfig(await res.json())
    } catch {
      /* ignore */
    }
  }, [])

  useEffect(() => {
    fetchPortfolio()
    fetchAlertConfig()
  }, [fetchPortfolio, fetchAlertConfig])

  // 60s auto-refresh (endpoint fetches live greeks)
  useEffect(() => {
    const interval = setInterval(fetchPortfolio, 60000)
    return () => clearInterval(interval)
  }, [fetchPortfolio])

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

  const sendTestEmail = async () => {
    setTestEmailStatus('Sending...')
    try {
      const res = await fetch(`${API}/api/risk/alerts/test-email`, { method: 'POST' })
      const data = await res.json()
      if (data.status === 'sent') {
        setTestEmailStatus(
          `Sent via ${data.auth_method === 'oauth2' ? 'OAuth2' : 'App Password'} to ${data.recipient}`
        )
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
          <p className="text-gray-400">Loading portfolio risk...</p>
        </div>
      </div>
    )
  }

  if (portfolioError || !portfolio) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Risk Dashboard</h2>
        <div className="bg-surface rounded-xl border border-border p-12 text-center">
          <p className="text-danger mb-1">Portfolio risk unavailable</p>
          <p className="text-xs text-gray-600">Backend unreachable — retrying every 60s</p>
        </div>
      </div>
    )
  }

  const p = portfolio
  const ex = p.exposures
  const books = Object.values(ex?.strategies || {})
  const scenarios = Object.entries(p.stress || {})
  const grossLevBudget = N(p.budgets?.max_gross_leverage)
  const levUtil = grossLevBudget > 0 ? (N(ex?.gross_leverage) / grossLevBudget) * 100 : 0

  /** Worst-case stress $ for one book across all scenarios (null if never shocked). */
  const worstStress = (sid: string): number | null => {
    const vals = scenarios
      .map(([, sc]) => sc.per_strategy_pnl_usd[sid])
      .filter((v): v is number => v !== undefined)
    return vals.length ? Math.min(...vals) : null
  }

  return (
    <div className="space-y-6">
      {/* ── Header ── */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Risk Dashboard</h2>
          <p className="text-xs text-gray-500">
            Universal book view — all strategies, one contract · advisory v1 · refreshes every 60s
          </p>
        </div>
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
      </div>

      {p.error && (
        <div className="bg-surface rounded-xl border border-rose-500/40 p-4">
          <p className="text-sm text-rose-400">Report degraded: {p.error}</p>
        </div>
      )}

      {/* ── 1. Combined header ── */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-6 items-start">
          <div>
            <span className="text-xs text-gray-500">Combined Equity ({books.length} books)</span>
            <p className="text-2xl font-bold text-success">${fmt(ex?.combined_equity_usd)}</p>
            <p className="text-[11px] text-gray-600 mt-0.5">
              Gross ${fmt0(ex?.combined_gross_notional_usd)} · Net ${fmt0(ex?.combined_net_notional_usd)}
            </p>
          </div>
          <div>
            <span className="text-xs text-gray-500">Combined Day P&L</span>
            <p className={`text-2xl font-bold ${signCls(N(ex?.combined_day_pnl_usd))}`}>
              {signStr(N(ex?.combined_day_pnl_usd))}
            </p>
            <p className="text-[11px] text-gray-600 mt-0.5">vs previous daily close, all books</p>
          </div>
          <div>
            <span className="text-xs text-gray-500">Gross Leverage</span>
            <p
              className={`text-2xl font-bold ${
                levUtil > 100 ? 'text-danger' : levUtil > 80 ? 'text-warning' : 'text-gray-100'
              }`}
            >
              {N(ex?.gross_leverage).toFixed(2)}x
            </p>
            <div className="h-1.5 mt-1.5 bg-background rounded-full overflow-hidden border border-border max-w-[160px]">
              <div
                className={`h-full rounded-full ${
                  levUtil > 100 ? 'bg-rose-500' : levUtil > 80 ? 'bg-amber-400' : 'bg-success'
                }`}
                style={{ width: `${Math.min(Math.max(levUtil, 1), 100)}%` }}
              />
            </div>
            <p className="text-[11px] text-gray-600 mt-0.5">budget {grossLevBudget.toFixed(1)}x</p>
          </div>
          <div>
            <span className="text-xs text-gray-500">Combined Drawdown (30d)</span>
            <div className="mt-1">
              <DrawdownSparkline curve={p.equity_curves?.combined || []} />
            </div>
          </div>
        </div>
      </div>

      {/* ── 2. Books table ── */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-1">Books</h3>
        <p className="text-xs text-gray-500 mb-4">
          One universal row per strategy book — click a row for type-specific detail
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-gray-500 border-b border-border">
                <th className="py-2 pr-3">Book</th>
                <th className="py-2 pr-3">Type</th>
                <th className="py-2 pr-3 text-right">Equity</th>
                <th className="py-2 pr-3 text-right">Day P&L</th>
                <th className="py-2 pr-3 text-right">Gross / Net</th>
                <th className="py-2 pr-3 text-right">Lev</th>
                <th className="py-2 pr-3">Rails</th>
                <th className="py-2 pr-3 text-right">Stress worst</th>
                <th className="py-2">Fresh</th>
              </tr>
            </thead>
            <tbody>
              {books.map((b) => {
                const degraded = !!b.error
                const worst = worstStress(b.strategy_id)
                const capUsed = b.rails?.daily_used_pct
                const isExpanded = expanded === b.strategy_id
                return (
                  <Fragment key={b.strategy_id}>
                    <tr
                      onClick={() => setExpanded(isExpanded ? null : b.strategy_id)}
                      className={`border-b border-border/50 cursor-pointer transition-colors hover:bg-background/60 ${
                        degraded ? 'opacity-50' : ''
                      }`}
                    >
                      <td className="py-2.5 pr-3">
                        <span className="font-semibold text-gray-100">{b.strategy_id}</span>
                        {p.flags?.[b.strategy_id] && (
                          <span className="ml-2 px-1.5 py-0.5 rounded text-[10px] font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/40">
                            {p.flags[b.strategy_id]}
                          </span>
                        )}
                        {degraded && (
                          <p className="text-[10px] text-amber-400/80 mt-0.5 max-w-[220px] truncate">
                            {b.error}
                          </p>
                        )}
                      </td>
                      <td className="py-2.5 pr-3">
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                            BOOK_TYPE_BADGE[b.book_type] || 'bg-gray-500/20 text-gray-400'
                          }`}
                        >
                          {b.book_type}
                        </span>{' '}
                        <span
                          className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                            ASSET_BADGE[b.asset] || 'bg-gray-500/20 text-gray-400'
                          }`}
                        >
                          {b.asset}
                        </span>
                      </td>
                      <td className="py-2.5 pr-3 text-right font-mono text-gray-100">
                        ${fmt(b.equity_usd)}
                      </td>
                      <td className={`py-2.5 pr-3 text-right font-mono ${signCls(N(b.day_pnl_usd))}`}>
                        {signStr(N(b.day_pnl_usd))}
                      </td>
                      <td className="py-2.5 pr-3 text-right font-mono text-gray-300">
                        ${fmt0(b.gross_notional_usd)}
                        <span className="text-gray-600"> / </span>
                        <span className={N(b.net_notional_usd) < 0 ? 'text-rose-300' : ''}>
                          ${fmt0(b.net_notional_usd)}
                        </span>
                      </td>
                      <td
                        className={`py-2.5 pr-3 text-right font-mono ${
                          N(b.leverage) > 3 ? 'text-warning' : 'text-gray-300'
                        }`}
                      >
                        {N(b.leverage).toFixed(2)}x
                      </td>
                      <td className="py-2.5 pr-3">
                        {b.rails?.halted ? (
                          <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/40">
                            HALTED
                          </span>
                        ) : capUsed != null ? (
                          <div className="min-w-[90px]">
                            <div className="h-1.5 bg-background rounded-full overflow-hidden border border-border">
                              <div
                                className={`h-full rounded-full ${
                                  capUsed > 0.8 ? 'bg-rose-500' : capUsed > 0.5 ? 'bg-amber-400' : 'bg-success'
                                }`}
                                style={{ width: `${Math.min(Math.max(capUsed * 100, 2), 100)}%` }}
                              />
                            </div>
                            <span className="text-[10px] text-gray-500">
                              cap {(capUsed * 100).toFixed(0)}% used
                            </span>
                          </div>
                        ) : (
                          <span className="text-[11px] text-gray-600">—</span>
                        )}
                      </td>
                      <td className="py-2.5 pr-3 text-right font-mono">
                        {worst == null ? (
                          <span className="text-gray-600">—</span>
                        ) : (
                          <span className={worst < 0 ? 'text-rose-300' : 'text-gray-400'}>
                            {signStr(worst)}
                          </span>
                        )}
                      </td>
                      <td className="py-2.5">
                        <span className="inline-flex items-center gap-1.5">
                          <span
                            className={`w-2 h-2 rounded-full ${
                              b.freshness?.fresh ? 'bg-success' : 'bg-amber-400'
                            }`}
                          />
                          <span className="text-[11px] text-gray-500">
                            {ageStr(b.freshness?.age_hours ?? null)}
                          </span>
                        </span>
                      </td>
                    </tr>
                    {isExpanded && (
                      <tr className="border-b border-border/50">
                        <td colSpan={9} className="bg-background/40">
                          <BookDetailPanel book={b} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* ── 3. Stress matrix ── */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-1">Stress Matrix</h3>
        <p className="text-xs text-gray-500 mb-4">
          Scenario P&L per book ($) — red intensity scales with loss as % of book equity. Options
          books revalued (BS / greeks expansion); linear books first-order (net notional x shock).
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wide text-gray-500 border-b border-border">
                <th className="py-2 pr-3">Scenario</th>
                {books.map((b) => (
                  <th key={b.strategy_id} className="py-2 px-2 text-right font-mono normal-case">
                    {b.strategy_id}
                  </th>
                ))}
                <th className="py-2 pl-3 text-right">Total</th>
              </tr>
            </thead>
            <tbody>
              {scenarios.map(([name, sc]) => (
                <tr key={name} className="border-b border-border/50">
                  <td className="py-2.5 pr-3">
                    <p className="font-semibold text-gray-100">{name}</p>
                    <p className="text-[10px] text-gray-500">{sc.description}</p>
                  </td>
                  {books.map((b) => {
                    const v = sc.per_strategy_pnl_usd[b.strategy_id]
                    if (v === undefined) {
                      return (
                        <td key={b.strategy_id} className="py-2.5 px-2 text-right text-gray-700">
                          —
                        </td>
                      )
                    }
                    const lossFrac =
                      v < 0 && b.equity_usd > 0 ? Math.min(Math.abs(v) / b.equity_usd / 0.1, 1) : 0
                    return (
                      <td
                        key={b.strategy_id}
                        className={`py-2.5 px-2 text-right font-mono ${
                          v < 0 ? 'text-rose-200' : 'text-gray-300'
                        }`}
                        style={
                          lossFrac > 0
                            ? { backgroundColor: `rgba(244, 63, 94, ${(0.08 + 0.5 * lossFrac).toFixed(3)})` }
                            : undefined
                        }
                      >
                        {signStr(v)}
                      </td>
                    )
                  })}
                  <td
                    className={`py-2.5 pl-3 text-right font-mono font-semibold ${signCls(
                      N(sc.pnl_usd)
                    )}`}
                  >
                    {signStr(N(sc.pnl_usd))}
                    <p className="text-[10px] font-normal text-gray-500">
                      {(N(sc.loss_pct) * 100).toFixed(1)}% of equity
                    </p>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {scenarios.some(([, sc]) => sc.errors?.length > 0) && (
          <p className="text-[11px] text-amber-400 mt-2">
            Stress errors:{' '}
            {scenarios.flatMap(([, sc]) => sc.errors || []).join(' · ')}
          </p>
        )}
      </div>

      {/* ── 4. Budget utilization + flags ── */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <div>
            <h3 className="text-lg font-semibold">Portfolio Budgets</h3>
            <p className="text-xs text-gray-500">
              Cross-strategy budgets — all books combined (advisory: breaches flag REDUCE_ONLY)
            </p>
          </div>
          <div className="flex items-center gap-3">
            {Object.entries(p.flags || {}).map(([sid, flag]) => (
              <span
                key={sid}
                className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-xs font-semibold bg-rose-500/20 text-rose-400 border border-rose-500/40"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-rose-400 animate-pulse" />
                {sid}: {flag}
              </span>
            ))}
            {Object.keys(p.flags || {}).length === 0 && (
              <span className="text-xs text-gray-600">No breaches — no flags raised</span>
            )}
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {(p.utilization || []).map((u) => {
            const pct = Math.min(N(u.utilization) * 100, 100)
            const barColor = u.breached ? 'bg-rose-500' : pct > 80 ? 'bg-amber-400' : 'bg-success'
            const usedStr =
              u.id === 'abs_vega'
                ? `$${fmt(u.used)}`
                : u.id === 'gross_leverage'
                ? `${N(u.used).toFixed(2)}x`
                : `${(N(u.used) * 100).toFixed(1)}%`
            const limitStr =
              u.id === 'abs_vega'
                ? `$${fmt(u.limit)} / vol-pt`
                : u.id === 'gross_leverage'
                ? `${N(u.limit).toFixed(1)}x equity`
                : `${(N(u.limit) * 100).toFixed(0)}% of equity`
            return (
              <div key={u.id} className="bg-background rounded-lg p-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-sm text-gray-100">{u.label}</span>
                  <span
                    className={`text-xs font-semibold ${
                      u.breached ? 'text-rose-400' : pct > 80 ? 'text-amber-400' : 'text-gray-400'
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

        <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
          <div className="flex items-center gap-2">
            {(p.degraded || []).map((sid) => (
              <span
                key={sid}
                className="text-[11px] px-2 py-0.5 rounded bg-amber-500/15 text-amber-400"
              >
                {sid}: data degraded
              </span>
            ))}
          </div>
          <span className="text-[11px] text-gray-600">
            Net vega ${fmt(ex?.net_vega_usd)}/vol-pt · updated {p.generated_at} · refreshes every 60s
          </span>
        </div>
      </div>

      {/* ── 5. Email alert configuration (bottom) ── */}
      {showAlertPanel && alertConfig && (
        <div className="bg-surface rounded-xl border border-accent/30 p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold">Email Alert Configuration</h3>
            <div className="flex items-center gap-3">
              {testEmailStatus && <span className="text-xs text-accent">{testEmailStatus}</span>}
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
              <p>
                <span className="text-success font-mono">OAuth2 (recommended):</span> python -m
                dashboard.alerts.email_notifier --setup
              </p>
              <p>
                <span className="text-warning font-mono">App Password (fallback):</span> Set
                ALERT_EMAIL_SENDER + ALERT_EMAIL_PASSWORD in .env
              </p>
              <p className="text-gray-500">
                OAuth2 only needs send permission, never stores your password.
              </p>
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
    </div>
  )
}
