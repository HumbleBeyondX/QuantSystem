'use client'

import Link from 'next/link'
import { useCallback, useEffect, useMemo, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8001'

// ── Types ────────────────────────────────────────────────────

interface TradingStatus {
  is_running: boolean
  data_source: string
  execution_mode: string
  last_bar_date: string | null
  spy_price: number
  vix: number
  implied_vol: number
  rv_20d: number
  vrp: number
  position_open: boolean
  trade_direction: string
  contracts: number
  strike: number
  hedge_shares: number
  realized_pnl: number
  unrealized_pnl: number
  total_equity: number
  initial_capital: number
  portfolio_delta: number
  portfolio_gamma: number
  portfolio_theta: number
  portfolio_vega: number
  total_trades: number
  consecutive_losses: number
  peak_equity: number
  broker_connected: boolean
  broker_equity: number | null
  broker_cash: number | null
}

interface DailySnapshot {
  date: string
  spy_close: number
  vix: number
  implied_vol: number
  rv_20d: number
  actions: string[]
  position_open: boolean
  trade_direction: string
  contracts: number
  realized_pnl: number
  unrealized_pnl: number
  total_equity: number
  total_trades: number
}

interface TradeRecord {
  timestamp: string
  direction: string
  reason: string
  entry_spot: number
  exit_spot: number
  strike: number
  entry_iv: number
  exit_iv: number
  contracts: number
  total_pnl: number
  option_pnl: number
  hedge_pnl: number
  gamma_pnl: number
  theta_pnl: number
  vega_pnl: number
  txn_costs: number
  running_total: number
}

interface EquityPoint {
  date: string
  equity: number
  drawdown: number
}

interface TradingConfig {
  available_sources: string[]
  strategies: Array<{
    id: string
    name: string
    description: string
    sharpe: number
    backtest_trades: number
    backtest_win_rate: number
  }>
  execution_modes: string[]
  state_exists: boolean
}

interface DeployedStrategy {
  id: string
  strategy_id: string
  name: string
  mode: string
  status: string
  data_source: string
  execution_broker: string
  initial_capital: number
  last_run: string | null
  realized_pnl: number
  unrealized_pnl: number
  total_equity: number
  total_trades: number
  current_position: string | null
  spy_price: number
}

interface WheelPosition {
  put: string
  call: string
  kp: number
  kc: number
  expiry_ms: number
  mult: number
  entry_index: number
  entry_bid_p: number
  entry_bid_c: number
  credit_total: number
  opened: string
}

interface WheelMark {
  date: string
  index: number
  action: string
  realized: number
  unrealized: number
  total_equity: number
  position_open: boolean
  put: string | null
  call: string | null
  n_trades: number
  wins: number
}

interface WheelTrade {
  ts: string
  action: string
  put?: string
  call?: string
  credit_total?: number
  pnl?: number
  intrinsic?: number
  settle_index?: number
}

interface WheelData {
  strategy_id: string
  state: {
    equity: number
    realized: number
    n_trades: number
    wins: number
    created: string
  }
  position: WheelPosition | null
  last_mark: WheelMark | null
  equity_curve: { date: string; equity: number }[]
  trades: WheelTrade[]
  n_trades: number
  wins: number
}

// ── Helper Components ────────────────────────────────────────

function MetricCard({ title, value, sub, color = 'text-gray-100' }: {
  title: string; value: string; sub?: string; color?: string
}) {
  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <p className="text-[11px] text-gray-500 uppercase tracking-wider">{title}</p>
      <p className={`text-lg font-bold mt-1 font-mono ${color}`}>{value}</p>
      {sub && <p className="text-[10px] text-gray-500 mt-0.5">{sub}</p>}
    </div>
  )
}

function EquityCurveChart({ data }: { data: EquityPoint[] }) {
  if (data.length < 2) {
    return (
      <div className="h-48 flex items-center justify-center text-gray-500 text-sm">
        Insufficient data for chart. Run paper trading for a few days.
      </div>
    )
  }

  const equities = data.map(d => d.equity)
  const minE = Math.min(...equities) * 0.998
  const maxE = Math.max(...equities) * 1.002
  const rangeE = maxE - minE || 1

  const w = 600
  const h = 160
  const pts = data.map((d, i) => ({
    x: (i / (data.length - 1)) * w,
    y: h - ((d.equity - minE) / rangeE) * h,
  }))
  const line = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
  const area = `${line} L${w},${h} L0,${h} Z`

  const isUp = equities[equities.length - 1] >= equities[0]
  const gradId = 'eq-grad'
  const strokeColor = isUp ? '#10b981' : '#ef4444'

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-48" preserveAspectRatio="none">
      <defs>
        <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0%" stopColor={strokeColor} stopOpacity="0.3" />
          <stop offset="100%" stopColor={strokeColor} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gradId})`} />
      <path d={line} fill="none" stroke={strokeColor} strokeWidth="2" />
    </svg>
  )
}

// ── Main Page ────────────────────────────────────────────────

export default function TradingPage() {
  const [tab, setTab] = useState<'monitor' | 'control'>('monitor')
  const [status, setStatus] = useState<TradingStatus | null>(null)
  const [dailyLog, setDailyLog] = useState<DailySnapshot[]>([])
  const [trades, setTrades] = useState<TradeRecord[]>([])
  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([])
  const [config, setConfig] = useState<TradingConfig | null>(null)
  const [deployed, setDeployed] = useState<DeployedStrategy[]>([])
  const [selectedStrategyId, setSelectedStrategyId] = useState<string>('')
  const [loading, setLoading] = useState(true)
  const [running, setRunning] = useState(false)
  const [selectedSource, setSelectedSource] = useState('alpaca')
  const [useExecution, setUseExecution] = useState(true)
  const [wheel, setWheel] = useState<WheelData | null>(null)

  // Fetch deployed strategies list
  const fetchDeployed = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/strategies/`)
      if (res.ok) {
        const data: DeployedStrategy[] = await res.json()
        setDeployed(data)
        // Auto-select first running strategy, or first strategy
        if (!selectedStrategyId && data.length > 0) {
          const running = data.find(d => d.status === 'running')
          setSelectedStrategyId(running?.strategy_id || data[0].strategy_id)
        }
      }
    } catch (e) {
      console.error('Failed to fetch deployed strategies:', e)
    }
  }, [selectedStrategyId])

  // Fetch data for the selected strategy (resilient: individual failures don't break everything)
  const fetchData = useCallback(async () => {
    if (!selectedStrategyId) return
    const sid = encodeURIComponent(selectedStrategyId)

    const safeFetch = async (url: string) => {
      try {
        const r = await fetch(url)
        if (!r.ok) return null
        return await r.json()
      } catch {
        return null
      }
    }

    try {
      const [statusRes, dailyRes, tradesRes, equityRes, configRes] = await Promise.all([
        safeFetch(`${API}/api/paper-trading/status?strategy_id=${sid}`),
        safeFetch(`${API}/api/paper-trading/daily-log?limit=100&strategy_id=${sid}`),
        safeFetch(`${API}/api/paper-trading/trades?limit=50&strategy_id=${sid}`),
        safeFetch(`${API}/api/paper-trading/equity-curve?strategy_id=${sid}`),
        safeFetch(`${API}/api/paper-trading/config?strategy_id=${sid}`),
      ])
      if (statusRes) setStatus(statusRes)
      if (dailyRes) setDailyLog(dailyRes)
      if (tradesRes) setTrades(tradesRes)
      if (equityRes) setEquityCurve(equityRes)
      if (configRes) setConfig(configRes)
    } catch (e) {
      console.error('Failed to fetch trading data:', e)
    } finally {
      setLoading(false)
    }
  }, [selectedStrategyId])

  // Fetch BTC wheel paper-trading status (404 = wheel never ran)
  const fetchWheel = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/paper-trading/btc-wheel`)
      if (!res.ok) {
        setWheel(null)
        return
      }
      setWheel(await res.json())
    } catch {
      setWheel(null)
    }
  }, [])

  useEffect(() => {
    fetchDeployed()
    fetchData()
    fetchWheel()
    const interval = setInterval(() => { fetchData(); fetchDeployed(); fetchWheel() }, 30000)
    return () => clearInterval(interval)
  }, [fetchData, fetchDeployed, fetchWheel])

  const handleRunOnce = async () => {
    setRunning(true)
    try {
      await fetch(`${API}/api/paper-trading/run-once`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          data_source: selectedSource,
          execution: useExecution,
          initial_capital: status?.initial_capital ?? 100000,
        }),
      })
      setTimeout(fetchData, 5000)
    } catch (e) {
      console.error('Failed to run:', e)
    } finally {
      setTimeout(() => setRunning(false), 5000)
    }
  }

  const handleReset = async () => {
    if (!confirm(`Reset ${selectedStrategyId} state? This will clear all history.`)) return
    try {
      await fetch(`${API}/api/paper-trading/reset?strategy_id=${encodeURIComponent(selectedStrategyId)}`, { method: 'POST' })
      fetchData()
    } catch (e) {
      console.error('Failed to reset:', e)
    }
  }

  // Derived
  const selectedDep = useMemo(() => deployed.find(d => d.strategy_id === selectedStrategyId), [deployed, selectedStrategyId])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="text-gray-500">Loading trading data...</div>
      </div>
    )
  }

  const s = status ?? {} as TradingStatus
  const totalPnl = (s.realized_pnl ?? 0) + (s.unrealized_pnl ?? 0)
  const pnlPct = s.initial_capital ? (totalPnl / s.initial_capital * 100) : 0

  return (
    <div className="space-y-6 max-w-[1400px]">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Paper Trading</h1>
          <p className="text-sm text-gray-500 mt-1">
            Live Monitoring & Control
          </p>
        </div>
        <div className="flex items-center gap-3">
          {/* Broker status */}
          <div className="flex items-center gap-2 px-3 py-1.5 bg-surface rounded-lg border border-border">
            <span className={`w-2 h-2 rounded-full ${s.broker_connected ? 'bg-success animate-pulse' : 'bg-gray-600'}`} />
            <span className="text-xs text-gray-400">
              {s.broker_connected ? `Alpaca $${(s.broker_equity ?? 0).toLocaleString()}` : 'Broker Offline'}
            </span>
          </div>
          {/* Tab toggle */}
          <div className="flex bg-surface rounded-lg border border-border overflow-hidden">
            <button
              onClick={() => setTab('monitor')}
              className={`px-4 py-1.5 text-xs font-medium transition-colors ${tab === 'monitor' ? 'bg-primary text-white' : 'text-gray-400 hover:text-gray-200'}`}
            >
              Monitor
            </button>
            <button
              onClick={() => setTab('control')}
              className={`px-4 py-1.5 text-xs font-medium transition-colors ${tab === 'control' ? 'bg-primary text-white' : 'text-gray-400 hover:text-gray-200'}`}
            >
              Control
            </button>
          </div>
        </div>
      </div>

      {/* Strategy Selector Bar */}
      {deployed.length > 0 && (
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-xs text-gray-500 mr-1">Strategy:</span>
          {deployed.map(dep => {
            const isActive = dep.strategy_id === selectedStrategyId
            const isRunning = dep.status === 'running'
            return (
              <button
                key={dep.id}
                onClick={() => setSelectedStrategyId(dep.strategy_id)}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium transition-all border ${
                  isActive
                    ? 'bg-primary/20 border-primary/50 text-primary'
                    : 'bg-surface border-border text-gray-400 hover:border-gray-500 hover:text-gray-200'
                }`}
              >
                <span className={`w-1.5 h-1.5 rounded-full ${
                  isRunning ? 'bg-green-500 animate-pulse' : 'bg-gray-500'
                }`} />
                <span>{dep.name}</span>
                <span className={`text-[10px] px-1 py-0.5 rounded ${
                  dep.mode === 'live' ? 'bg-red-500/20 text-red-400' : 'bg-yellow-500/20 text-yellow-400'
                }`}>
                  {dep.mode}
                </span>
                {dep.realized_pnl !== 0 && (
                  <span className={`font-mono ${dep.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {dep.realized_pnl >= 0 ? '+' : ''}${dep.realized_pnl.toFixed(0)}
                  </span>
                )}
              </button>
            )
          })}
          {/* BTC wheel paper runner (jsonl telemetry, not a deployment record) */}
          <button
            onClick={() => setSelectedStrategyId('btc_wheel')}
            className={`flex items-center gap-2 px-3 py-1.5 rounded-lg text-xs font-medium transition-all border ${
              selectedStrategyId === 'btc_wheel'
                ? 'bg-primary/20 border-primary/50 text-primary'
                : 'bg-surface border-border text-gray-400 hover:border-gray-500 hover:text-gray-200'
            }`}
          >
            <span className={`w-1.5 h-1.5 rounded-full ${wheel ? 'bg-green-500 animate-pulse' : 'bg-gray-500'}`} />
            <span>BTC Wheel</span>
            <span className="text-[10px] px-1 py-0.5 rounded bg-yellow-500/20 text-yellow-400">paper</span>
            {wheel && (wheel.last_mark?.unrealized ?? 0) + (wheel.last_mark?.realized ?? 0) !== 0 && (
              <span className={`font-mono ${
                (wheel.last_mark?.realized ?? 0) + (wheel.last_mark?.unrealized ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'
              }`}>
                {(wheel.last_mark?.realized ?? 0) + (wheel.last_mark?.unrealized ?? 0) >= 0 ? '+' : ''}
                ${((wheel.last_mark?.realized ?? 0) + (wheel.last_mark?.unrealized ?? 0)).toFixed(0)}
              </span>
            )}
          </button>
        </div>
      )}

      {tab === 'monitor' ? (
        <>
          {selectedStrategyId !== 'btc_wheel' && (<>
          {/* ── SPY Gamma Scalp ── */}
          <h2 className="text-base font-semibold text-gray-200 border-b border-border pb-2">
            SPY Gamma Scalp &mdash; paper (Alpaca OPRA)
          </h2>

          {/* ── KPI Cards ── */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
            <MetricCard
              title="Total Equity"
              value={`$${(s.total_equity ?? 100000).toLocaleString(undefined, { minimumFractionDigits: 0 })}`}
              sub={`${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(2)}% all time`}
              color={totalPnl >= 0 ? 'text-success' : 'text-danger'}
            />
            <MetricCard
              title="Realized P&L"
              value={`${s.realized_pnl >= 0 ? '+' : ''}$${(s.realized_pnl ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
              color={s.realized_pnl >= 0 ? 'text-success' : 'text-danger'}
            />
            <MetricCard
              title="Unrealized P&L"
              value={`${s.unrealized_pnl >= 0 ? '+' : ''}$${(s.unrealized_pnl ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
              color={s.unrealized_pnl >= 0 ? 'text-success' : 'text-danger'}
            />
            <MetricCard
              title="SPY Price"
              value={`$${(s.spy_price ?? 0).toFixed(2)}`}
              sub={`Last: ${s.last_bar_date ?? 'N/A'}`}
            />
            <MetricCard
              title="VIX"
              value={(s.vix ?? 0).toFixed(2)}
              sub={`IV: ${((s.implied_vol ?? 0) * 100).toFixed(1)}%`}
              color={s.vix > 30 ? 'text-danger' : s.vix > 20 ? 'text-warning' : 'text-success'}
            />
            <MetricCard
              title="VRP (IV - RV)"
              value={`${s.vrp >= 0 ? '+' : ''}${((s.vrp ?? 0) * 100).toFixed(1)}%`}
              sub={`RV20: ${((s.rv_20d ?? 0) * 100).toFixed(1)}%`}
              color={s.vrp > 0 ? 'text-success' : 'text-danger'}
            />
          </div>

          {/* ── Position & Greeks ── */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            {/* Active Position */}
            <div className="bg-surface rounded-xl border border-border p-5">
              <h3 className="text-sm font-semibold text-gray-300 mb-4">Active Position</h3>
              {s.position_open ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-2 mb-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-bold ${
                      s.trade_direction === 'sell_vol'
                        ? 'bg-danger/20 text-danger'
                        : 'bg-success/20 text-success'
                    }`}>
                      {s.trade_direction === 'sell_vol' ? 'SHORT STRADDLE' : 'LONG STRADDLE'}
                    </span>
                    <span className="text-xs text-gray-500">SPY</span>
                  </div>
                  <div className="grid grid-cols-2 gap-2 text-sm">
                    <div><span className="text-gray-500">Strike:</span> <span className="font-mono">${s.strike}</span></div>
                    <div><span className="text-gray-500">Contracts:</span> <span className="font-mono">{s.contracts}</span></div>
                    <div><span className="text-gray-500">Hedge:</span> <span className="font-mono">{s.hedge_shares} shr</span></div>
                    <div><span className="text-gray-500">Trades:</span> <span className="font-mono">{s.total_trades}</span></div>
                  </div>
                </div>
              ) : (
                <div className="text-center py-6">
                  <p className="text-gray-500 text-sm">No active position</p>
                  <p className="text-[11px] text-gray-600 mt-1">
                    Waiting for signal (composite &gt; threshold)
                  </p>
                </div>
              )}
            </div>

            {/* Greeks & risk metrics live on the Portfolio Risk page */}
            <div className="lg:col-span-2 flex items-center">
              <Link href="/risk" className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
                Portfolio greeks &amp; risk limits &rarr; Portfolio Risk
              </Link>
            </div>
          </div>

          {/* ── Equity Curve ── */}
          <div className="bg-surface rounded-xl border border-border p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-3">Equity Curve</h3>
            <EquityCurveChart data={equityCurve} />
            {equityCurve.length > 0 && (
              <div className="flex justify-between text-[10px] text-gray-600 mt-1 px-1">
                <span>{equityCurve[0]?.date}</span>
                <span>{equityCurve[equityCurve.length - 1]?.date}</span>
              </div>
            )}
          </div>

          {/* ── Daily Activity Log ── */}
          <div className="bg-surface rounded-xl border border-border p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-3">Daily Activity Log</h3>
            <div className="overflow-x-auto max-h-64 overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-surface">
                  <tr className="text-gray-500 border-b border-border">
                    <th className="text-left py-2 px-2">Date</th>
                    <th className="text-right px-2">SPY</th>
                    <th className="text-right px-2">VIX</th>
                    <th className="text-right px-2">VRP</th>
                    <th className="text-left px-2">Action</th>
                    <th className="text-left px-2">Position</th>
                    <th className="text-right px-2">Equity</th>
                    <th className="text-right px-2">P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {[...dailyLog].reverse().map((d, i) => (
                    <tr key={i} className="border-b border-border/30 hover:bg-background/50 transition-colors">
                      <td className="py-1.5 px-2 font-mono">{d.date}</td>
                      <td className="text-right px-2 font-mono">${d.spy_close?.toFixed(2)}</td>
                      <td className="text-right px-2 font-mono">{d.vix?.toFixed(1)}</td>
                      <td className="text-right px-2 font-mono">
                        <span className={(d.implied_vol - d.rv_20d) > 0 ? 'text-success' : 'text-danger'}>
                          {((d.implied_vol - d.rv_20d) * 100).toFixed(1)}%
                        </span>
                      </td>
                      <td className="px-2">
                        {d.actions?.map((a: string, j: number) => (
                          <span key={j} className={`px-1.5 py-0.5 rounded text-[10px] mr-1 ${
                            a.includes('OPEN') ? 'bg-primary/20 text-primary' :
                            a.includes('CLOSE') ? 'bg-warning/20 text-warning' :
                            a.includes('HEDGE') ? 'bg-accent/20 text-accent' :
                            'bg-gray-700/50 text-gray-400'
                          }`}>
                            {a}
                          </span>
                        ))}
                      </td>
                      <td className="px-2">
                        {d.position_open ? (
                          <span className="text-[10px] bg-danger/20 text-danger px-1 rounded">
                            {d.trade_direction} x{d.contracts}
                          </span>
                        ) : (
                          <span className="text-gray-600">flat</span>
                        )}
                      </td>
                      <td className="text-right px-2 font-mono">${d.total_equity?.toLocaleString()}</td>
                      <td className={`text-right px-2 font-mono ${d.realized_pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                        {d.realized_pnl >= 0 ? '+' : ''}${d.realized_pnl?.toFixed(0)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {dailyLog.length === 0 && (
                <p className="text-center text-gray-500 py-6 text-sm">No daily logs yet. Run paper trading to generate data.</p>
              )}
            </div>
          </div>

          {/* ── Trade History ── */}
          <div className="bg-surface rounded-xl border border-border p-5">
            <h3 className="text-sm font-semibold text-gray-300 mb-3">
              Trade History
              {trades.length > 0 && <span className="text-gray-500 font-normal ml-2">({trades.length} trades)</span>}
            </h3>
            <div className="overflow-x-auto max-h-72 overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-surface">
                  <tr className="text-gray-500 border-b border-border">
                    <th className="text-left py-2 px-2">Date</th>
                    <th className="text-left px-2">Type</th>
                    <th className="text-left px-2">Reason</th>
                    <th className="text-right px-2">Entry</th>
                    <th className="text-right px-2">Exit</th>
                    <th className="text-right px-2">Contracts</th>
                    <th className="text-right px-2">Option P&L</th>
                    <th className="text-right px-2">Hedge P&L</th>
                    <th className="text-right px-2">Costs</th>
                    <th className="text-right px-2 font-semibold">Total P&L</th>
                    <th className="text-right px-2">Cumulative</th>
                  </tr>
                </thead>
                <tbody>
                  {[...trades].reverse().map((t, i) => (
                    <tr key={i} className="border-b border-border/30 hover:bg-background/50 transition-colors">
                      <td className="py-1.5 px-2 font-mono">{t.timestamp?.split('T')[0]}</td>
                      <td className="px-2">
                        <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                          t.direction === 'sell_vol' ? 'bg-danger/20 text-danger' : 'bg-success/20 text-success'
                        }`}>
                          {t.direction}
                        </span>
                      </td>
                      <td className="px-2 text-gray-400">{t.reason}</td>
                      <td className="text-right px-2 font-mono">${t.entry_spot?.toFixed(2)}</td>
                      <td className="text-right px-2 font-mono">${t.exit_spot?.toFixed(2)}</td>
                      <td className="text-right px-2 font-mono">{t.contracts}</td>
                      <td className={`text-right px-2 font-mono ${t.option_pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                        {t.option_pnl >= 0 ? '+' : ''}${t.option_pnl?.toFixed(0)}
                      </td>
                      <td className={`text-right px-2 font-mono ${t.hedge_pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                        {t.hedge_pnl >= 0 ? '+' : ''}${t.hedge_pnl?.toFixed(0)}
                      </td>
                      <td className="text-right px-2 font-mono text-gray-500">-${t.txn_costs?.toFixed(0)}</td>
                      <td className={`text-right px-2 font-mono font-semibold ${t.total_pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                        {t.total_pnl >= 0 ? '+' : ''}${t.total_pnl?.toFixed(0)}
                      </td>
                      <td className={`text-right px-2 font-mono ${t.running_total >= 0 ? 'text-success' : 'text-danger'}`}>
                        ${t.running_total?.toLocaleString()}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {trades.length === 0 && (
                <p className="text-center text-gray-500 py-6 text-sm">No trades yet. Strategy will trade when signal threshold is met.</p>
              )}
            </div>
          </div>

          </>)}

          {/* ── BTC Wheel ── */}
          {selectedStrategyId === 'btc_wheel' && (<>
          <h2 className="text-base font-semibold text-gray-200 border-b border-border pb-2">
            BTC Wheel &mdash; paper (real Deribit chains)
          </h2>

          {wheel ? (
            <>
              {/* Summary Row */}
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
                <MetricCard
                  title="Total Equity"
                  value={`$${(wheel.last_mark?.total_equity ?? wheel.state.equity).toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
                  color={(wheel.last_mark?.total_equity ?? wheel.state.equity) >= wheel.state.equity ? 'text-success' : 'text-danger'}
                />
                <MetricCard
                  title="Realized P&L"
                  value={`${(wheel.last_mark?.realized ?? wheel.state.realized) >= 0 ? '+' : ''}$${(wheel.last_mark?.realized ?? wheel.state.realized).toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
                  color={(wheel.last_mark?.realized ?? wheel.state.realized) >= 0 ? 'text-success' : 'text-danger'}
                />
                <MetricCard
                  title="Unrealized P&L"
                  value={`${(wheel.last_mark?.unrealized ?? 0) >= 0 ? '+' : ''}$${(wheel.last_mark?.unrealized ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
                  color={(wheel.last_mark?.unrealized ?? 0) >= 0 ? 'text-success' : 'text-danger'}
                />
                <MetricCard
                  title="BTC Index"
                  value={wheel.last_mark ? `$${wheel.last_mark.index.toLocaleString(undefined, { minimumFractionDigits: 1 })}` : 'N/A'}
                  sub={`Last: ${wheel.last_mark?.date ?? 'N/A'}`}
                />
                <div className="bg-surface rounded-xl border border-border p-4">
                  <p className="text-[11px] text-gray-500 uppercase tracking-wider">Last Action</p>
                  <span className={`inline-block mt-1.5 px-2 py-0.5 rounded text-xs font-bold ${
                    wheel.last_mark?.action === 'OPEN' ? 'bg-primary/20 text-primary' :
                    wheel.last_mark?.action === 'SETTLE' ? 'bg-warning/20 text-warning' :
                    'bg-gray-700/50 text-gray-400'
                  }`}>
                    {wheel.last_mark?.action ?? 'N/A'}
                  </span>
                  <p className="text-[10px] text-gray-500 mt-1">
                    {wheel.n_trades} trades &middot; {wheel.wins} wins
                  </p>
                </div>
              </div>

              {/* Open Position */}
              {wheel.position && (
                <div className="bg-surface rounded-xl border border-border p-5">
                  <h3 className="text-sm font-semibold text-gray-300 mb-4">Open Position — Short Strangle</h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-4">
                    <div className="bg-background rounded-lg p-3">
                      <span className="px-2 py-0.5 rounded text-xs font-bold bg-danger/20 text-danger">
                        SHORT PUT {wheel.position.kp.toLocaleString()}
                      </span>
                      <p className="font-mono text-sm mt-2 text-gray-200">{wheel.position.put}</p>
                    </div>
                    <div className="bg-background rounded-lg p-3">
                      <span className="px-2 py-0.5 rounded text-xs font-bold bg-danger/20 text-danger">
                        SHORT CALL {wheel.position.kc.toLocaleString()}
                      </span>
                      <p className="font-mono text-sm mt-2 text-gray-200">{wheel.position.call}</p>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-sm">
                    <div><span className="text-gray-500">Entry index:</span> <span className="font-mono">${wheel.position.entry_index.toLocaleString(undefined, { minimumFractionDigits: 1 })}</span></div>
                    <div><span className="text-gray-500">Total credit:</span> <span className="font-mono text-success">${wheel.position.credit_total.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span></div>
                    <div>
                      <span className="text-gray-500">Expiry:</span>{' '}
                      <span className="font-mono">
                        {new Date(wheel.position.expiry_ms).toISOString().split('T')[0]}
                        {' '}({Math.max(0, Math.ceil((wheel.position.expiry_ms - Date.now()) / 86400000))}d)
                      </span>
                    </div>
                    <div><span className="text-gray-500">Multiplier:</span> <span className="font-mono">{wheel.position.mult.toFixed(4)}</span></div>
                  </div>
                </div>
              )}

              {/* Equity Curve */}
              <div className="bg-surface rounded-xl border border-border p-5">
                <h3 className="text-sm font-semibold text-gray-300 mb-3">Equity Curve</h3>
                <EquityCurveChart data={wheel.equity_curve.map(p => ({ ...p, drawdown: 0 }))} />
                {wheel.equity_curve.length > 0 && (
                  <div className="flex justify-between text-[10px] text-gray-600 mt-1 px-1">
                    <span>{wheel.equity_curve[0]?.date}</span>
                    <span>{wheel.equity_curve[wheel.equity_curve.length - 1]?.date}</span>
                  </div>
                )}
              </div>

              {/* Trades */}
              <div className="bg-surface rounded-xl border border-border p-5">
                <h3 className="text-sm font-semibold text-gray-300 mb-3">
                  Trade History
                  {wheel.trades.length > 0 && <span className="text-gray-500 font-normal ml-2">({wheel.trades.length} trades)</span>}
                </h3>
                <div className="overflow-x-auto max-h-72 overflow-y-auto">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-surface">
                      <tr className="text-gray-500 border-b border-border">
                        <th className="text-left py-2 px-2">Date</th>
                        <th className="text-left px-2">Action</th>
                        <th className="text-left px-2">Instruments</th>
                        <th className="text-right px-2">Credit / P&L</th>
                      </tr>
                    </thead>
                    <tbody>
                      {[...wheel.trades].reverse().map((t, i) => (
                        <tr key={i} className="border-b border-border/30 hover:bg-background/50 transition-colors">
                          <td className="py-1.5 px-2 font-mono">{t.ts?.split('T')[0]}</td>
                          <td className="px-2">
                            <span className={`px-1.5 py-0.5 rounded text-[10px] ${
                              t.action === 'OPEN' ? 'bg-primary/20 text-primary' :
                              (t.pnl ?? 0) >= 0 ? 'bg-success/20 text-success' : 'bg-danger/20 text-danger'
                            }`}>
                              {t.action}
                            </span>
                          </td>
                          <td className="px-2 font-mono text-gray-400">
                            {[t.put, t.call].filter(Boolean).join(' / ') || '--'}
                          </td>
                          <td className={`text-right px-2 font-mono ${
                            t.action === 'OPEN' ? 'text-primary' : (t.pnl ?? 0) >= 0 ? 'text-success' : 'text-danger'
                          }`}>
                            {t.action === 'OPEN'
                              ? `+$${(t.credit_total ?? 0).toFixed(2)}`
                              : `${(t.pnl ?? 0) >= 0 ? '+' : ''}$${(t.pnl ?? 0).toFixed(2)}`
                            }
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {wheel.trades.length === 0 && (
                    <p className="text-center text-gray-500 py-6 text-sm">No wheel trades yet.</p>
                  )}
                </div>
              </div>

              <p className="text-[10px] text-gray-600">
                Shadow fills at real Deribit bids &middot; marks at real mark prices &middot; settlement at intrinsic. First weekly settlement 2026-07-17.
              </p>
            </>
          ) : (
            <p className="text-sm text-gray-500">BTC wheel not papering yet.</p>
          )}
          </>)}
        </>
      ) : (
        /* ── Control Panel Tab ── */
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          {/* Strategy Selector */}
          <div className="bg-surface rounded-xl border border-border p-6">
            <h3 className="text-sm font-semibold text-gray-300 mb-4">Strategy Configuration</h3>

            {config?.strategies.map(strat => (
              <div key={strat.id} className="p-4 bg-background rounded-lg border border-border mb-4">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="font-semibold text-sm">{strat.name}</h4>
                  <span className="text-xs bg-primary/20 text-primary px-2 py-0.5 rounded">
                    Sharpe {strat.sharpe}
                  </span>
                </div>
                <p className="text-xs text-gray-500 mb-3">{strat.description}</p>
                <div className="flex gap-4 text-xs text-gray-400">
                  <span>Trades: {strat.backtest_trades}</span>
                  <span>Win Rate: {(strat.backtest_win_rate * 100).toFixed(0)}%</span>
                </div>
              </div>
            ))}

            {/* Data Source */}
            <div className="mt-4">
              <label className="text-xs text-gray-500 mb-2 block">Data Source</label>
              <div className="flex gap-2">
                {(config?.available_sources ?? ['yahoo']).map(src => (
                  <button
                    key={src}
                    onClick={() => setSelectedSource(src)}
                    className={`px-4 py-2 rounded-lg text-xs font-medium transition-colors border ${
                      selectedSource === src
                        ? 'bg-primary/20 text-primary border-primary/50'
                        : 'bg-background text-gray-400 border-border hover:border-gray-600'
                    }`}
                  >
                    {src === 'yahoo' ? 'Yahoo Finance' : src === 'alpaca' ? 'Alpaca Paper' : 'IB TWS'}
                  </button>
                ))}
              </div>
            </div>

            {/* Execution Toggle */}
            <div className="mt-4 flex items-center justify-between">
              <div>
                <label className="text-xs text-gray-500">Broker Execution</label>
                <p className="text-[10px] text-gray-600">Route delta hedge orders to broker</p>
              </div>
              <button
                onClick={() => setUseExecution(!useExecution)}
                className={`w-12 h-6 rounded-full transition-colors ${
                  useExecution ? 'bg-primary' : 'bg-gray-700'
                }`}
              >
                <div className={`w-5 h-5 rounded-full bg-white shadow-md transform transition-transform ${
                  useExecution ? 'translate-x-6' : 'translate-x-0.5'
                }`} />
              </button>
            </div>
          </div>

          {/* Action Buttons */}
          <div className="bg-surface rounded-xl border border-border p-6">
            <h3 className="text-sm font-semibold text-gray-300 mb-4">Trading Actions</h3>

            <div className="space-y-3">
              {/* Run Once */}
              <button
                onClick={handleRunOnce}
                disabled={running}
                className={`w-full py-3 rounded-lg text-sm font-semibold transition-colors ${
                  running
                    ? 'bg-gray-700 text-gray-500 cursor-wait'
                    : 'bg-primary hover:bg-primary-light text-white'
                }`}
              >
                {running ? 'Running...' : 'Run Paper Trading (One Bar)'}
              </button>

              <p className="text-[10px] text-gray-600 text-center">
                Fetches latest market data, feeds to strategy, executes signals
              </p>

              <hr className="border-border" />

              {/* Reset */}
              <button
                onClick={handleReset}
                className="w-full py-2.5 rounded-lg text-sm font-medium bg-background border border-border text-gray-400 hover:text-danger hover:border-danger/50 transition-colors"
              >
                Reset State (Start Fresh)
              </button>

              {/* Status Info */}
              <div className="mt-4 p-3 bg-background rounded-lg">
                <h4 className="text-xs font-semibold text-gray-400 mb-2">Current State</h4>
                <div className="space-y-1.5 text-[11px]">
                  <div className="flex justify-between">
                    <span className="text-gray-500">State File</span>
                    <span className={config?.state_exists ? 'text-success' : 'text-gray-600'}>
                      {config?.state_exists ? 'Exists' : 'Not found'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Last Bar</span>
                    <span className="text-gray-300 font-mono">{s.last_bar_date ?? 'N/A'}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Broker</span>
                    <span className={s.broker_connected ? 'text-success' : 'text-gray-600'}>
                      {s.broker_connected ? 'Connected' : 'Disconnected'}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Source</span>
                    <span className="text-gray-300">{selectedSource}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Execution</span>
                    <span className={useExecution ? 'text-primary' : 'text-gray-600'}>
                      {useExecution ? 'Enabled' : 'Internal Only'}
                    </span>
                  </div>
                </div>
              </div>

              {/* Quick Guide */}
              <div className="mt-3 p-3 bg-background rounded-lg border border-border/50">
                <h4 className="text-xs font-semibold text-gray-400 mb-2">Quick Guide</h4>
                <ul className="text-[10px] text-gray-500 space-y-1 list-disc list-inside">
                  <li>Strategy checks VRP signal daily at market close</li>
                  <li>Sells ATM straddles when IV &gt;&gt; RV (VRP positive)</li>
                  <li>Delta hedges with SPY shares via broker</li>
                  <li>Avg trade duration: ~5 days (weekly straddles)</li>
                  <li>Backtest Sharpe: 1.98 | Win Rate: 80%</li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
