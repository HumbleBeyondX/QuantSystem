'use client'

import Link from 'next/link'
import { useState, useEffect } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'

/**
 * Strategies Page — API-Driven
 *
 * Fetches real strategy catalog from backend.
 * Shows deployment status, live P&L for deployed strategies,
 * backtest metrics for undeployed strategies.
 */

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
  realized_pnl: number
  unrealized_pnl: number
  total_equity: number
  total_trades: number
  win_rate: number | null
}

const assetClasses = ['All', 'crypto', 'equity', 'options'] as const
const assetClassLabels: Record<string, string> = {
  All: 'All',
  crypto: 'Crypto',
  equity: 'Equity',
  options: 'Options',
}

export default function StrategiesPage() {
  const [filter, setFilter] = useState<string>('All')
  const [catalog, setCatalog] = useState<CatalogStrategy[]>([])
  const [deployed, setDeployed] = useState<DeployedStrategy[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    async function fetchData() {
      try {
        const [catRes, depRes] = await Promise.all([
          fetch(`${API}/api/strategies/catalog`),
          fetch(`${API}/api/strategies/`),
        ])
        if (catRes.ok) setCatalog(await catRes.json())
        else setError('Failed to load strategy catalog')
        if (depRes.ok) setDeployed(await depRes.json())
      } catch (e: any) {
        setError(`Cannot connect to backend: ${e.message}`)
      } finally {
        setLoading(false)
      }
    }
    fetchData()
  }, [])

  const deployedMap = new Map(deployed.map((d) => [d.strategy_id, d]))
  const filtered = filter === 'All' ? catalog : catalog.filter((s) => s.asset_class === filter)

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Strategies</h2>
        <span className="text-sm text-gray-400">
          {loading ? '...' : `${filtered.length} strategies`}
        </span>
      </div>

      {/* Filter Tabs */}
      <div className="flex gap-2">
        {assetClasses.map((ac) => (
          <button
            key={ac}
            onClick={() => setFilter(ac)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              filter === ac
                ? 'bg-accent text-white'
                : 'bg-surface border border-border text-gray-400 hover:text-gray-100'
            }`}
          >
            {assetClassLabels[ac]}
          </button>
        ))}
      </div>

      {/* Workshop Entry */}
      <Link
        href="/workshop"
        className="bg-gradient-to-r from-accent/10 to-purple-500/10 rounded-xl border border-accent/30 hover:border-accent/60 p-5 flex items-center justify-between transition-all group"
      >
        <div className="flex items-center gap-4">
          <div className="w-12 h-12 rounded-xl bg-accent/20 flex items-center justify-center text-2xl">
            🔧
          </div>
          <div>
            <h3 className="font-semibold text-gray-100 group-hover:text-accent transition-colors">
              Strategy Workshop
            </h3>
            <p className="text-xs text-gray-500 mt-0.5">
              Create, edit, and test strategies &mdash; Monaco editor + templates for Options, Futures, HFT
            </p>
          </div>
        </div>
        <span className="text-accent text-sm font-medium opacity-0 group-hover:opacity-100 transition-opacity">
          Open &rarr;
        </span>
      </Link>

      {/* Error State */}
      {error && (
        <div className="bg-danger/10 border border-danger/30 rounded-xl p-4 text-sm text-danger">
          {error}
        </div>
      )}

      {/* Loading State */}
      {loading && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {[...Array(6)].map((_, i) => (
            <div key={i} className="bg-surface rounded-xl border border-border p-5 animate-pulse h-48" />
          ))}
        </div>
      )}

      {/* Strategy Cards Grid */}
      {!loading && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {filtered.map((strategy) => {
            const dep = deployedMap.get(strategy.id)
            const isDeployed = !!dep
            const isRunning = dep?.status === 'running'
            const pnl = dep ? dep.realized_pnl + dep.unrealized_pnl : null

            return (
              <Link
                key={strategy.id}
                href={`/strategies/${strategy.id}`}
                className="bg-surface rounded-xl border border-border p-5 hover:border-accent/50 transition-all group"
              >
                <div className="flex items-start justify-between mb-3">
                  <div>
                    <h3 className="font-semibold text-gray-100 group-hover:text-accent transition-colors">
                      {strategy.name}
                    </h3>
                    <div className="flex items-center gap-2 mt-1">
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
                      {strategy.symbols.length > 0 && (
                        <span className="text-[10px] text-gray-500">
                          {strategy.symbols.slice(0, 3).join(', ')}
                          {strategy.symbols.length > 3 && ` +${strategy.symbols.length - 3}`}
                        </span>
                      )}
                    </div>
                  </div>
                  {isDeployed ? (
                    <span
                      className={`flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-full ${
                        isRunning ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning'
                      }`}
                    >
                      <span
                        className={`w-1.5 h-1.5 rounded-full ${isRunning ? 'bg-success animate-pulse' : 'bg-warning'}`}
                      />
                      {isRunning ? 'Running' : dep.status.charAt(0).toUpperCase() + dep.status.slice(1)}
                    </span>
                  ) : (
                    <span className="text-[10px] text-gray-500 px-2 py-1 rounded-full bg-background border border-border">
                      Available
                    </span>
                  )}
                </div>

                <p className="text-xs text-gray-500 mb-4 line-clamp-2">{strategy.description}</p>

                <div className="grid grid-cols-3 gap-3 pt-3 border-t border-border">
                  {pnl !== null && dep ? (
                    <>
                      <div>
                        <p className="text-xs text-gray-500">P&L</p>
                        <p className={`text-sm font-semibold ${pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                          {pnl >= 0 ? '+' : ''}${pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-gray-500">Win Rate</p>
                        <p className="text-sm font-semibold text-gray-100">
                          {dep.win_rate != null ? `${dep.win_rate}%` : '\u2014'}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-gray-500">Trades</p>
                        <p className="text-sm font-semibold text-gray-100">{dep.total_trades}</p>
                      </div>
                    </>
                  ) : (
                    <>
                      <div>
                        <p className="text-xs text-gray-500">Sharpe</p>
                        <p className="text-sm font-semibold text-gray-100">
                          {strategy.backtest_sharpe?.toFixed(2) ?? '\u2014'}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-gray-500">Win Rate</p>
                        <p className="text-sm font-semibold text-gray-100">
                          {strategy.backtest_win_rate
                            ? `${(strategy.backtest_win_rate * 100).toFixed(0)}%`
                            : '\u2014'}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-gray-500">Backtest</p>
                        <p className="text-sm font-semibold text-gray-100">
                          {strategy.backtest_trades ?? '\u2014'} trades
                        </p>
                      </div>
                    </>
                  )}
                </div>
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}
