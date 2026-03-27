'use client'

import { useState, useEffect, useCallback, useMemo } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8001'

/* ── types ─────────────────────────────────────────────── */
interface Overview {
  trading_mode: string
  total_equity: number
  initial_capital: number
  total_return_pct: number
  realized_pnl: number
  unrealized_pnl: number
  max_drawdown_pct: number
  sharpe_ratio: number
  total_strategies: number
  running_strategies: number
  stopped_strategies: number
  total_trades: number
  win_rate: number | null
  broker_connected: boolean
  broker_equity: number | null
  last_updated: string
}

interface CatalogStrategy {
  id: string
  name: string
  description: string
  asset_class: string
  category: string
  symbols: string[]
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
  spy_price: number
}

/* ── main component ─────────────────────────────────────── */
export default function DashboardPage() {
  const [overview, setOverview] = useState<Overview | null>(null)
  const [catalog, setCatalog] = useState<CatalogStrategy[]>([])
  const [deployed, setDeployed] = useState<DeployedStrategy[]>([])
  const [loading, setLoading] = useState(true)
  const [lastUpdate, setLastUpdate] = useState('')

  // Deploy modal state
  const [showDeploy, setShowDeploy] = useState(false)
  const [deployStrategy, setDeployStrategy] = useState<CatalogStrategy | null>(null)
  const [deployMode, setDeployMode] = useState<'paper' | 'live'>('paper')
  const [deploySource, setDeploySource] = useState('alpaca')
  const [deployBroker, setDeployBroker] = useState('internal')
  const [deployCapital, setDeployCapital] = useState(100000)
  const [deploying, setDeploying] = useState(false)

  // Adjust modal state
  const [showAdjust, setShowAdjust] = useState(false)
  const [adjustTarget, setAdjustTarget] = useState<DeployedStrategy | null>(null)
  const [adjustMode, setAdjustMode] = useState('paper')
  const [adjustSource, setAdjustSource] = useState('alpaca')
  const [adjustBroker, setAdjustBroker] = useState('internal')

  /* ── data fetching ──────────────────────────────────── */
  const fetchAll = useCallback(async () => {
    try {
      const [ovRes, catRes, depRes] = await Promise.all([
        fetch(`${API}/api/system/overview`),
        fetch(`${API}/api/strategies/catalog`),
        fetch(`${API}/api/strategies/`),
      ])
      if (ovRes.ok) setOverview(await ovRes.json())
      if (catRes.ok) setCatalog(await catRes.json())
      if (depRes.ok) setDeployed(await depRes.json())
      setLastUpdate(new Date().toLocaleTimeString())
    } catch (e) {
      console.error('Fetch error:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const iv = setInterval(fetchAll, 15000)
    return () => clearInterval(iv)
  }, [fetchAll])

  /* ── actions ─────────────────────────────────────────── */
  const handleDeploy = async () => {
    if (!deployStrategy) return
    setDeploying(true)
    try {
      const res = await fetch(`${API}/api/strategies/deploy`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy_id: deployStrategy.id,
          mode: deployMode,
          data_source: deploySource,
          execution_broker: deployBroker,
          initial_capital: deployCapital,
        }),
      })
      if (res.ok) {
        setShowDeploy(false)
        await fetchAll()
      } else {
        const err = await res.json()
        alert(err.detail || 'Deploy failed')
      }
    } catch (e: any) {
      alert(`Deploy error: ${e.message}`)
    } finally {
      setDeploying(false)
    }
  }

  const handleStart = async (deployId: string) => {
    try {
      await fetch(`${API}/api/strategies/${deployId}/start`, { method: 'POST' })
      await fetchAll()
    } catch (e) { console.error(e) }
  }

  const handleStop = async (deployId: string) => {
    try {
      await fetch(`${API}/api/strategies/${deployId}/stop`, { method: 'POST' })
      await fetchAll()
    } catch (e) { console.error(e) }
  }

  const handleRemove = async (deployId: string) => {
    if (!confirm('Remove this strategy deployment?')) return
    try {
      await fetch(`${API}/api/strategies/${deployId}`, { method: 'DELETE' })
      await fetchAll()
    } catch (e) { console.error(e) }
  }

  const handleAdjust = async () => {
    if (!adjustTarget) return
    try {
      await fetch(`${API}/api/strategies/${adjustTarget.id}/adjust`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode: adjustMode,
          data_source: adjustSource,
          execution_broker: adjustBroker,
        }),
      })
      setShowAdjust(false)
      await fetchAll()
    } catch (e) { console.error(e) }
  }

  const openAdjustModal = (dep: DeployedStrategy) => {
    setAdjustTarget(dep)
    setAdjustMode(dep.mode)
    setAdjustSource(dep.data_source)
    setAdjustBroker(dep.execution_broker)
    setShowAdjust(true)
  }

  /* ── derived ──────────────────────────────────────────── */
  const deployedIds = useMemo(() => new Set(deployed.map(d => d.strategy_id)), [deployed])
  const tradingMode = overview?.trading_mode || 'paper'
  const modeColor = tradingMode === 'live' ? 'text-red-400' : tradingMode === 'paper' ? 'text-yellow-400' : 'text-blue-400'
  const modeBg = tradingMode === 'live' ? 'bg-red-500/20' : tradingMode === 'paper' ? 'bg-yellow-500/20' : 'bg-blue-500/20'
  const modeDot = tradingMode === 'live' ? 'bg-red-500' : 'bg-yellow-400'

  const fmt = (n: number, d = 2) => n?.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) ?? '—'

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Dashboard Overview</h2>
        <div className="flex items-center gap-4">
          <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg ${modeBg}`}>
            <span className={`w-2 h-2 rounded-full ${modeDot} animate-pulse`} />
            <span className={`text-sm font-medium ${modeColor} uppercase`}>
              {tradingMode === 'paper' ? 'Paper Trading' : tradingMode === 'live' ? 'Live Trading' : 'Mixed Mode'}
            </span>
          </div>
          {overview?.broker_connected && (
            <div className="flex items-center gap-2 text-xs text-gray-400">
              <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
              Alpaca: ${fmt(overview.broker_equity || 0, 0)}
            </div>
          )}
          <span className="text-xs text-gray-500">Last: {lastUpdate}</span>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KPICard
          label="Portfolio Value"
          value={`$${fmt(overview?.total_equity || 100000, 0)}`}
          sub={`Initial: $${fmt(overview?.initial_capital || 100000, 0)}`}
          color="text-gray-100"
        />
        <KPICard
          label="Total Return"
          value={`${(overview?.total_return_pct ?? 0) >= 0 ? '+' : ''}${fmt(overview?.total_return_pct || 0)}%`}
          sub={`Realized P&L: $${fmt(overview?.realized_pnl || 0)}`}
          color={(overview?.total_return_pct ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}
        />
        <KPICard
          label="Max Drawdown"
          value={`-${fmt(overview?.max_drawdown_pct || 0)}%`}
          sub="Peak to trough"
          color="text-red-400"
        />
        <KPICard
          label="Win Rate"
          value={overview?.win_rate != null ? `${overview.win_rate}%` : '—'}
          sub={`${overview?.total_trades || 0} total trades`}
          color="text-blue-400"
        />
      </div>

      {/* Strategy Deployment Section */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h3 className="text-lg font-semibold">Strategy Deployments</h3>
            <p className="text-xs text-gray-500 mt-1">
              {overview?.running_strategies || 0} running · {overview?.stopped_strategies || 0} stopped · {overview?.total_strategies || 0} total
            </p>
          </div>
          <button
            onClick={() => setShowDeploy(true)}
            className="px-4 py-2 bg-accent hover:bg-accent/80 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-2"
          >
            <span className="text-lg leading-none">+</span> Deploy Strategy
          </button>
        </div>

        {/* Deployed strategies table */}
        {deployed.length > 0 ? (
          <div className="space-y-3">
            {deployed.map((dep) => (
              <StrategyRow
                key={dep.id}
                dep={dep}
                onStart={() => handleStart(dep.id)}
                onStop={() => handleStop(dep.id)}
                onAdjust={() => openAdjustModal(dep)}
                onRemove={() => handleRemove(dep.id)}
              />
            ))}
          </div>
        ) : (
          <div className="text-center py-12 text-gray-500">
            <p className="text-4xl mb-3">📊</p>
            <p className="text-sm">No strategies deployed yet.</p>
            <p className="text-xs mt-1">Click &quot;Deploy Strategy&quot; to get started.</p>
          </div>
        )}
      </div>

      {/* Strategy Catalog */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-4">Strategy Catalog</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {catalog.map((strat) => {
            const isDeployed = deployedIds.has(strat.id)
            return (
              <div key={strat.id} className="bg-background rounded-lg border border-border p-4 hover:border-accent/50 transition-colors">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-sm font-semibold text-gray-100">{strat.name}</h4>
                  <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium uppercase ${
                    strat.category === 'options' ? 'bg-purple-500/20 text-purple-400' :
                    strat.category === 'equity' ? 'bg-blue-500/20 text-blue-400' :
                    'bg-orange-500/20 text-orange-400'
                  }`}>
                    {strat.category}
                  </span>
                </div>
                <p className="text-xs text-gray-500 mb-3 line-clamp-2">{strat.description}</p>
                <div className="flex items-center justify-between text-xs">
                  <div className="flex gap-3">
                    {strat.backtest_sharpe && (
                      <span className="text-gray-400">SR <span className="text-gray-200 font-mono">{strat.backtest_sharpe.toFixed(2)}</span></span>
                    )}
                    {strat.backtest_win_rate && (
                      <span className="text-gray-400">WR <span className="text-gray-200 font-mono">{(strat.backtest_win_rate * 100).toFixed(0)}%</span></span>
                    )}
                    {strat.backtest_trades && (
                      <span className="text-gray-400">{strat.backtest_trades} trades</span>
                    )}
                  </div>
                  {isDeployed ? (
                    <span className="text-green-400 text-[10px] font-medium px-2 py-0.5 rounded bg-green-500/10">DEPLOYED</span>
                  ) : (
                    <button
                      onClick={() => {
                        setDeployStrategy(strat)
                        setShowDeploy(true)
                      }}
                      className="text-accent hover:text-accent/80 font-medium transition-colors"
                    >
                      Deploy →
                    </button>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Deploy Modal */}
      {showDeploy && deployStrategy && (
        <Modal title={`Deploy: ${deployStrategy.name}`} onClose={() => setShowDeploy(false)}>
          <div className="space-y-4">
            <div>
              <label className="text-xs text-gray-400 block mb-1.5">Trading Mode</label>
              <div className="flex gap-2">
                <ModeButton active={deployMode === 'paper'} onClick={() => setDeployMode('paper')} color="yellow">
                  📋 Paper Trading
                </ModeButton>
                <ModeButton active={deployMode === 'live'} onClick={() => setDeployMode('live')} color="red">
                  🔴 Live Trading
                </ModeButton>
              </div>
              {deployMode === 'live' && (
                <p className="text-[10px] text-red-400 mt-1.5">⚠ Live trading uses real money. Ensure broker is configured.</p>
              )}
            </div>

            <div>
              <label className="text-xs text-gray-400 block mb-1.5">Data Source</label>
              <select value={deploySource} onChange={e => setDeploySource(e.target.value)}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100">
                <option value="yahoo">Yahoo Finance (Free)</option>
                <option value="alpaca">Alpaca API (Real-time)</option>
                <option value="ib">Interactive Brokers</option>
              </select>
            </div>

            <div>
              <label className="text-xs text-gray-400 block mb-1.5">Execution Broker</label>
              <select value={deployBroker} onChange={e => setDeployBroker(e.target.value)}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100">
                <option value="internal">Internal (Simulated)</option>
                <option value="alpaca">Alpaca Paper/Live</option>
                <option value="ib">Interactive Brokers TWS</option>
              </select>
            </div>

            <div>
              <label className="text-xs text-gray-400 block mb-1.5">Initial Capital ($)</label>
              <input type="number" value={deployCapital} onChange={e => setDeployCapital(Number(e.target.value))}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 font-mono" />
            </div>

            <div className="flex gap-3 pt-2">
              <button onClick={() => setShowDeploy(false)}
                className="flex-1 px-4 py-2 bg-background border border-border rounded-lg text-sm text-gray-400 hover:text-gray-100 transition-colors">
                Cancel
              </button>
              <button onClick={handleDeploy} disabled={deploying}
                className="flex-1 px-4 py-2 bg-accent hover:bg-accent/80 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50">
                {deploying ? 'Deploying...' : `Deploy (${deployMode})`}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {/* Adjust Modal */}
      {showAdjust && adjustTarget && (
        <Modal title={`Adjust: ${adjustTarget.name}`} onClose={() => setShowAdjust(false)}>
          <div className="space-y-4">
            <div>
              <label className="text-xs text-gray-400 block mb-1.5">Trading Mode</label>
              <div className="flex gap-2">
                <ModeButton active={adjustMode === 'paper'} onClick={() => setAdjustMode('paper')} color="yellow">
                  📋 Paper
                </ModeButton>
                <ModeButton active={adjustMode === 'live'} onClick={() => setAdjustMode('live')} color="red">
                  🔴 Live
                </ModeButton>
              </div>
            </div>

            <div>
              <label className="text-xs text-gray-400 block mb-1.5">Data Source</label>
              <select value={adjustSource} onChange={e => setAdjustSource(e.target.value)}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100">
                <option value="yahoo">Yahoo Finance</option>
                <option value="alpaca">Alpaca API</option>
                <option value="ib">Interactive Brokers</option>
              </select>
            </div>

            <div>
              <label className="text-xs text-gray-400 block mb-1.5">Execution Broker</label>
              <select value={adjustBroker} onChange={e => setAdjustBroker(e.target.value)}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100">
                <option value="internal">Internal (Simulated)</option>
                <option value="alpaca">Alpaca</option>
                <option value="ib">Interactive Brokers</option>
              </select>
            </div>

            <div className="flex gap-3 pt-2">
              <button onClick={() => setShowAdjust(false)}
                className="flex-1 px-4 py-2 bg-background border border-border rounded-lg text-sm text-gray-400 hover:text-gray-100 transition-colors">
                Cancel
              </button>
              <button onClick={handleAdjust}
                className="flex-1 px-4 py-2 bg-accent hover:bg-accent/80 text-white rounded-lg text-sm font-medium transition-colors">
                Save Changes
              </button>
            </div>
          </div>
        </Modal>
      )}
    </div>
  )
}


/* ── Strategy Row Component ──────────────────────────────── */
function StrategyRow({ dep, onStart, onStop, onAdjust, onRemove }: {
  dep: DeployedStrategy
  onStart: () => void
  onStop: () => void
  onAdjust: () => void
  onRemove: () => void
}) {
  const isRunning = dep.status === 'running'
  const pnl = dep.realized_pnl + dep.unrealized_pnl
  const returnPct = dep.initial_capital > 0
    ? ((dep.total_equity - dep.initial_capital) / dep.initial_capital * 100)
    : 0

  return (
    <div className={`bg-background rounded-lg border p-4 transition-colors ${
      isRunning ? 'border-green-500/30' : 'border-border'
    }`}>
      <div className="flex items-center justify-between">
        {/* Left: name + status */}
        <div className="flex items-center gap-3">
          <div className={`w-2.5 h-2.5 rounded-full ${
            isRunning ? 'bg-green-500 animate-pulse' :
            dep.status === 'error' ? 'bg-red-500' :
            'bg-gray-500'
          }`} />
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-gray-100">{dep.name}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium uppercase ${
                dep.mode === 'live'
                  ? 'bg-red-500/20 text-red-400'
                  : 'bg-yellow-500/20 text-yellow-400'
              }`}>
                {dep.mode}
              </span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface text-gray-500 uppercase">
                {dep.data_source}
              </span>
            </div>
            <div className="text-[11px] text-gray-500 mt-0.5 flex items-center gap-3">
              <span>Position: {dep.current_position || '—'}</span>
              {dep.last_run && <span>Last: {dep.last_run}</span>}
            </div>
          </div>
        </div>

        {/* Center: metrics */}
        <div className="flex items-center gap-6 text-xs">
          <div className="text-center">
            <p className="text-gray-500">Equity</p>
            <p className="text-gray-100 font-mono font-medium">${dep.total_equity.toLocaleString(undefined, { maximumFractionDigits: 0 })}</p>
          </div>
          <div className="text-center">
            <p className="text-gray-500">P&L</p>
            <p className={`font-mono font-medium ${pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {pnl >= 0 ? '+' : ''}${pnl.toLocaleString(undefined, { maximumFractionDigits: 0 })}
            </p>
          </div>
          <div className="text-center">
            <p className="text-gray-500">Return</p>
            <p className={`font-mono font-medium ${returnPct >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {returnPct >= 0 ? '+' : ''}{returnPct.toFixed(2)}%
            </p>
          </div>
          <div className="text-center">
            <p className="text-gray-500">Trades</p>
            <p className="text-gray-100 font-mono">{dep.total_trades}</p>
          </div>
          {dep.win_rate != null && (
            <div className="text-center">
              <p className="text-gray-500">Win Rate</p>
              <p className="text-gray-100 font-mono">{dep.win_rate}%</p>
            </div>
          )}
        </div>

        {/* Right: controls */}
        <div className="flex items-center gap-2">
          {isRunning ? (
            <button onClick={onStop}
              className="px-3 py-1.5 bg-red-500/20 hover:bg-red-500/30 text-red-400 rounded-lg text-xs font-medium transition-colors">
              ⏹ Stop
            </button>
          ) : (
            <button onClick={onStart}
              className="px-3 py-1.5 bg-green-500/20 hover:bg-green-500/30 text-green-400 rounded-lg text-xs font-medium transition-colors">
              ▶ Start
            </button>
          )}
          <button onClick={onAdjust}
            className="px-3 py-1.5 bg-surface hover:bg-background border border-border text-gray-400 hover:text-gray-100 rounded-lg text-xs font-medium transition-colors">
            ⚙ Adjust
          </button>
          {!isRunning && (
            <button onClick={onRemove}
              className="px-2 py-1.5 text-gray-500 hover:text-red-400 rounded-lg text-xs transition-colors"
              title="Remove deployment">
              ✕
            </button>
          )}
        </div>
      </div>
    </div>
  )
}


/* ── KPI Card ──────────────────────────────────────────── */
function KPICard({ label, value, sub, color = 'text-gray-100' }: {
  label: string; value: string; sub?: string; color?: string
}) {
  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-xl font-bold mt-1 font-mono ${color}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-0.5">{sub}</p>}
    </div>
  )
}


/* ── Modal ──────────────────────────────────────────────── */
function Modal({ title, children, onClose }: {
  title: string; children: React.ReactNode; onClose: () => void
}) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-surface border border-border rounded-xl shadow-2xl w-full max-w-md mx-4 p-6">
        <div className="flex items-center justify-between mb-5">
          <h3 className="text-lg font-semibold">{title}</h3>
          <button onClick={onClose} className="text-gray-500 hover:text-gray-100 text-lg transition-colors">✕</button>
        </div>
        {children}
      </div>
    </div>
  )
}


/* ── Mode Toggle Button ──────────────────────────────────── */
function ModeButton({ active, onClick, color, children }: {
  active: boolean; onClick: () => void; color: string; children: React.ReactNode
}) {
  const colors = {
    yellow: active ? 'border-yellow-500/50 bg-yellow-500/10 text-yellow-400' : 'border-border text-gray-400 hover:text-gray-100',
    red: active ? 'border-red-500/50 bg-red-500/10 text-red-400' : 'border-border text-gray-400 hover:text-gray-100',
  }
  return (
    <button onClick={onClick}
      className={`flex-1 px-3 py-2 rounded-lg border text-sm font-medium transition-colors ${colors[color as keyof typeof colors]}`}>
      {children}
    </button>
  )
}
