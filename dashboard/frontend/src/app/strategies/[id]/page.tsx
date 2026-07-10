'use client'

import Link from 'next/link'
import { useState, useEffect } from 'react'
import { useParams } from 'next/navigation'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'

/**
 * Strategy Detail Page — API-Driven
 *
 * Fetches real strategy data from backend catalog,
 * deployment status, signals history, and equity curve.
 * Provides workflow actions: Deploy, Backtest, Live Trading.
 */

interface CatalogStrategy {
  id: string
  name: string
  description: string
  asset_class: string
  category: string
  symbols: string[]
  default_params: Record<string, any>
  backtest_sharpe: number | null
  backtest_win_rate: number | null
  backtest_trades: number | null
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
  deployed_at: string
  last_run: string | null
  realized_pnl: number
  unrealized_pnl: number
  total_equity: number
  total_trades: number
  win_rate: number | null
  current_position: string | null
}

interface Signal {
  date: string
  vrp: number
  implied_vol: number
  rv_20d: number
  actions: string[]
  position_open: boolean
  direction: string
}

interface EquityPoint {
  date: string
  equity: number
  drawdown: number
}

export default function StrategyDetailPage() {
  const params = useParams()
  const id = params.id as string

  const [strategy, setStrategy] = useState<CatalogStrategy | null>(null)
  const [deployment, setDeployment] = useState<DeployedStrategy | null>(null)
  const [signals, setSignals] = useState<Signal[]>([])
  const [equityCurve, setEquityCurve] = useState<EquityPoint[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    async function fetchData() {
      try {
        // Fetch catalog strategy info
        const catRes = await fetch(`${API}/api/strategies/catalog/${id}`)
        if (!catRes.ok) {
          setError(`Strategy "${id}" not found`)
          setLoading(false)
          return
        }
        const catData = await catRes.json()
        setStrategy(catData)

        // Fetch deployments to find if this strategy is deployed
        const depRes = await fetch(`${API}/api/strategies/`)
        if (depRes.ok) {
          const deps: DeployedStrategy[] = await depRes.json()
          const dep = deps.find((d) => d.strategy_id === id)
          if (dep) {
            setDeployment(dep)

            // Fetch equity curve for deployed strategy
            const eqRes = await fetch(`${API}/api/strategies/${dep.id}/equity-curve`)
            if (eqRes.ok) setEquityCurve(await eqRes.json())
          }
        }

        // Fetch signals
        const sigRes = await fetch(`${API}/api/strategies/${id}/signals`)
        if (sigRes.ok) setSignals(await sigRes.json())
      } catch (e: any) {
        setError(`Cannot connect to backend: ${e.message}`)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [id])

  if (loading) {
    return (
      <div className="space-y-6">
        <Link href="/strategies" className="text-accent hover:underline text-sm">
          &larr; Back to Strategies
        </Link>
        <div className="space-y-4">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="bg-surface rounded-xl border border-border p-6 animate-pulse h-32" />
          ))}
        </div>
      </div>
    )
  }

  if (error || !strategy) {
    return (
      <div className="space-y-4">
        <Link href="/strategies" className="text-accent hover:underline text-sm">
          &larr; Back to Strategies
        </Link>
        <div className="bg-surface rounded-xl border border-border p-12 text-center">
          <p className="text-gray-400">{error || `Strategy "${id}" not found.`}</p>
        </div>
      </div>
    )
  }

  const isDeployed = !!deployment
  const isRunning = deployment?.status === 'running'
  const pnl = deployment ? deployment.realized_pnl + deployment.unrealized_pnl : 0
  const returnPct =
    deployment && deployment.initial_capital > 0
      ? ((deployment.total_equity - deployment.initial_capital) / deployment.initial_capital) * 100
      : 0

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <Link href="/strategies" className="text-accent hover:underline text-sm">
            &larr; Back to Strategies
          </Link>
          <h2 className="text-2xl font-bold mt-2">{strategy.name}</h2>
          <div className="flex items-center gap-3 mt-1">
            <span
              className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                strategy.category === 'crypto'
                  ? 'bg-purple-500/20 text-purple-400'
                  : strategy.category === 'equity'
                    ? 'bg-blue-500/20 text-blue-400'
                    : 'bg-amber-500/20 text-amber-400'
              }`}
            >
              {strategy.category.charAt(0).toUpperCase() + strategy.category.slice(1)}
            </span>
            <span className="text-xs text-gray-500">{strategy.symbols.join(', ')}</span>
            {isDeployed && (
              <span
                className={`flex items-center gap-1.5 text-xs font-medium ${isRunning ? 'text-success' : 'text-warning'}`}
              >
                <span
                  className={`w-1.5 h-1.5 rounded-full ${isRunning ? 'bg-success animate-pulse' : 'bg-warning'}`}
                />
                {isRunning ? 'Running' : deployment.status}
                <span className="text-gray-600 ml-1">
                  ({deployment.mode === 'live' ? 'LIVE' : 'PAPER'})
                </span>
              </span>
            )}
          </div>
          <p className="text-sm text-gray-500 mt-2 max-w-2xl">{strategy.description}</p>
        </div>

        {/* Workflow Actions */}
        <div className="flex items-center gap-2">
          <Link
            href={`/backtest?strategy=${strategy.id}&symbol=${encodeURIComponent(strategy.symbols[0] || '')}`}
            className="px-4 py-2 bg-surface border border-border text-gray-300 rounded-lg text-sm font-medium hover:border-accent/50 hover:text-accent transition-colors"
          >
            Run Backtest
          </Link>
          {isDeployed ? (
            <Link
              href="/cloud"
              className="px-4 py-2 bg-accent/10 border border-accent/30 text-accent rounded-lg text-sm font-medium hover:bg-accent/20 transition-colors"
            >
              Ops &middot; Live &rarr;
            </Link>
          ) : (
            <Link
              href="/"
              className="px-4 py-2 bg-accent hover:bg-accent/80 text-white rounded-lg text-sm font-medium transition-colors"
            >
              Deploy Strategy
            </Link>
          )}
        </div>
      </div>

      {/* Performance Metrics Row */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        {isDeployed ? (
          <>
            <MetricCard
              title="P&L"
              value={`${pnl >= 0 ? '+' : ''}$${pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
              color={pnl >= 0 ? 'text-success' : 'text-danger'}
            />
            <MetricCard
              title="Return"
              value={`${returnPct >= 0 ? '+' : ''}${returnPct.toFixed(2)}%`}
              color={returnPct >= 0 ? 'text-success' : 'text-danger'}
            />
            <MetricCard title="Equity" value={`$${deployment.total_equity.toLocaleString()}`} />
            <MetricCard
              title="Win Rate"
              value={deployment.win_rate != null ? `${deployment.win_rate}%` : '\u2014'}
            />
            <MetricCard title="Total Trades" value={deployment.total_trades.toString()} />
            <MetricCard title="Position" value={deployment.current_position || 'Flat'} />
          </>
        ) : (
          <>
            <MetricCard
              title="Backtest Sharpe"
              value={strategy.backtest_sharpe?.toFixed(2) ?? '\u2014'}
              color="text-blue-400"
            />
            <MetricCard
              title="Backtest Win Rate"
              value={strategy.backtest_win_rate ? `${(strategy.backtest_win_rate * 100).toFixed(1)}%` : '\u2014'}
            />
            <MetricCard
              title="Backtest Trades"
              value={strategy.backtest_trades?.toString() ?? '\u2014'}
            />
            <MetricCard title="Asset Class" value={strategy.asset_class.toUpperCase()} />
            <MetricCard title="Symbols" value={strategy.symbols.length.toString()} />
            <MetricCard title="Status" value="Not Deployed" color="text-gray-500" />
          </>
        )}
      </div>

      {/* Deployment Info */}
      {isDeployed && (
        <div className="bg-surface rounded-xl border border-border p-5">
          <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Deployment Info</h3>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4 text-sm">
            <div>
              <p className="text-xs text-gray-500">Mode</p>
              <p className={`font-medium ${deployment.mode === 'live' ? 'text-red-400' : 'text-yellow-400'}`}>
                {deployment.mode.toUpperCase()}
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Data Source</p>
              <p className="text-gray-100">{deployment.data_source}</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Broker</p>
              <p className="text-gray-100">{deployment.execution_broker}</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Initial Capital</p>
              <p className="text-gray-100 font-mono">${deployment.initial_capital.toLocaleString()}</p>
            </div>
            <div>
              <p className="text-xs text-gray-500">Deployed At</p>
              <p className="text-gray-100">{new Date(deployment.deployed_at).toLocaleDateString()}</p>
            </div>
          </div>
        </div>
      )}

      {/* Equity Curve */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-4">Equity Curve</h3>
        <div className="h-64 flex items-center justify-center relative">
          {equityCurve.length > 0 ? (
            <EquityChart data={equityCurve} />
          ) : (
            <div className="text-center text-gray-500">
              <p className="text-sm">
                {isDeployed ? 'No equity data yet. Start the strategy to generate data.' : 'Deploy this strategy to see live equity curve.'}
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Strategy Parameters */}
      {Object.keys(strategy.default_params).length > 0 && (
        <div className="bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">Default Parameters</h3>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {Object.entries(strategy.default_params).map(([key, val]) => (
              <div key={key} className="bg-background rounded-lg border border-border p-3">
                <p className="text-xs text-gray-500 font-mono">{key}</p>
                <p className="text-sm font-semibold text-gray-100 mt-1 font-mono">
                  {typeof val === 'number' ? val.toLocaleString() : String(val)}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Signals History */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-4">Signals History</h3>
        {signals.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-border">
                  <th className="text-left pb-3 font-medium">Date</th>
                  <th className="text-left pb-3 font-medium">Direction</th>
                  <th className="text-right pb-3 font-medium">VRP</th>
                  <th className="text-right pb-3 font-medium">IV</th>
                  <th className="text-right pb-3 font-medium">RV</th>
                  <th className="text-center pb-3 font-medium">Position</th>
                  <th className="text-left pb-3 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {signals.slice(0, 20).map((sig, i) => (
                  <tr key={i} className="border-b border-border/50 hover:bg-background/50 transition-colors">
                    <td className="py-3 text-gray-400 font-mono text-xs">{sig.date}</td>
                    <td className="py-3">
                      <span
                        className={`px-2 py-0.5 rounded text-xs font-medium ${
                          sig.direction === 'long'
                            ? 'bg-success/10 text-success'
                            : sig.direction === 'short'
                              ? 'bg-danger/10 text-danger'
                              : 'bg-gray-500/10 text-gray-400'
                        }`}
                      >
                        {sig.direction.toUpperCase()}
                      </span>
                    </td>
                    <td className="py-3 text-right font-mono text-xs text-gray-300">
                      {(sig.vrp * 100).toFixed(1)}%
                    </td>
                    <td className="py-3 text-right font-mono text-xs text-gray-400">
                      {(sig.implied_vol * 100).toFixed(1)}%
                    </td>
                    <td className="py-3 text-right font-mono text-xs text-gray-400">
                      {(sig.rv_20d * 100).toFixed(1)}%
                    </td>
                    <td className="py-3 text-center">
                      <span
                        className={`w-2 h-2 inline-block rounded-full ${sig.position_open ? 'bg-success' : 'bg-gray-600'}`}
                      />
                    </td>
                    <td className="py-3 text-xs text-gray-400">
                      {sig.actions.length > 0 ? sig.actions.join(', ') : '\u2014'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="text-center py-8 text-gray-500">
            <p className="text-sm">No signals recorded yet.</p>
            <p className="text-xs mt-1">
              {isDeployed ? 'Signals will appear as the strategy runs.' : 'Deploy and run this strategy to generate signals.'}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Equity Chart ──────────────────────────────────────── */
function EquityChart({ data }: { data: EquityPoint[] }) {
  if (data.length < 2) return <p className="text-gray-500 text-sm">Insufficient data</p>

  const equities = data.map((d) => d.equity)
  const minEq = Math.min(...equities) * 0.995
  const maxEq = Math.max(...equities) * 1.005
  const range = maxEq - minEq || 1

  const w = 800
  const h = 200
  const pts = data.map((d, i) => {
    const x = (i / (data.length - 1)) * w
    const y = h - ((d.equity - minEq) / range) * h
    return `${x},${y}`
  })

  return (
    <svg className="w-full h-full" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#3b82f6" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#3b82f6" stopOpacity="0" />
        </linearGradient>
      </defs>
      {[0, 1, 2, 3, 4].map((i) => (
        <line key={i} x1="0" y1={i * (h / 4)} x2={w} y2={i * (h / 4)} stroke="#1f2937" strokeWidth="1" />
      ))}
      <path d={`M${pts.join(' L')} L${w},${h} L0,${h} Z`} fill="url(#eqGrad)" />
      <path d={`M${pts.join(' L')}`} fill="none" stroke="#3b82f6" strokeWidth="2" />
    </svg>
  )
}

/* ── Metric Card ──────────────────────────────────────── */
function MetricCard({ title, value, color = 'text-gray-100' }: { title: string; value: string; color?: string }) {
  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <p className="text-xs text-gray-500">{title}</p>
      <p className={`text-lg font-bold mt-1 ${color}`}>{value}</p>
    </div>
  )
}
