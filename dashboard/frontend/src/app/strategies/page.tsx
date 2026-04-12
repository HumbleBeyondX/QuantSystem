'use client'

import Link from 'next/link'
import { useState } from 'react'

/**
 * Strategies Page
 *
 * Grid of strategy cards with filtering by asset class.
 * Each card links to the strategy detail page.
 */

const strategies = [
  {
    id: 'crypto-macro-cycle',
    name: 'BTC/ETH Macro Cycle',
    assetClass: 'Crypto',
    state: 'running' as const,
    pnl: 62_525,
    winRate: 62.6,
    sharpe: 0.41,
    trades: 222,
    description: '5x leveraged macro cycle entry signals (Wide Stops). Multi-factor composite (trend, momentum, volume, drawdown, volatility regime). 4H timeframe, max 3 trades/day. Return +62.5%, MaxDD 10.6%, PF 1.62.',
  },
  {
    id: 'momentum-btc',
    name: 'BTC Momentum',
    assetClass: 'Crypto',
    state: 'running' as const,
    pnl: 12_450.30,
    winRate: 62.5,
    sharpe: 1.85,
    trades: 148,
    description: 'Trend-following momentum strategy on BTC/USD with adaptive lookback.',
  },
  {
    id: 'mean-rev-eth',
    name: 'ETH Mean Reversion',
    assetClass: 'Crypto',
    state: 'running' as const,
    pnl: 5_230.00,
    winRate: 58.3,
    sharpe: 1.42,
    trades: 210,
    description: 'Statistical mean reversion on ETH using Bollinger Bands and RSI.',
  },
  {
    id: 'pairs-tech',
    name: 'Tech Pairs Trading',
    assetClass: 'Equity',
    state: 'paused' as const,
    pnl: -1_820.50,
    winRate: 45.0,
    sharpe: 0.65,
    trades: 92,
    description: 'Cointegrated pairs trading on FAANG stocks.',
  },
  {
    id: 'spy-gamma-scalp',
    name: 'SPY Gamma Scalp',
    assetClass: 'Options',
    state: 'running' as const,
    pnl: 8_920.75,
    winRate: 71.2,
    sharpe: 2.10,
    trades: 325,
    description: 'Gamma scalping strategy on SPY 0DTE options.',
  },
  {
    id: 'vol-arb',
    name: 'Volatility Arb',
    assetClass: 'Options',
    state: 'paused' as const,
    pnl: 3_100.00,
    winRate: 55.8,
    sharpe: 1.20,
    trades: 67,
    description: 'Implied vs realized volatility arbitrage across index options.',
  },
  {
    id: 'sector-rotation',
    name: 'Sector Rotation',
    assetClass: 'Equity',
    state: 'running' as const,
    pnl: 6_780.25,
    winRate: 60.0,
    sharpe: 1.55,
    trades: 44,
    description: 'Monthly sector rotation based on relative strength and macro signals.',
  },
]

const assetClasses = ['All', 'Crypto', 'Equity', 'Options'] as const

export default function StrategiesPage() {
  const [filter, setFilter] = useState<string>('All')

  const filtered = filter === 'All' ? strategies : strategies.filter(s => s.assetClass === filter)

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Strategies</h2>
        <span className="text-sm text-gray-400">{filtered.length} strategies</span>
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
            {ac}
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

      {/* Strategy Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {filtered.map((strategy) => (
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
                <span className={`inline-block mt-1 px-2 py-0.5 rounded text-xs font-medium ${
                  strategy.assetClass === 'Crypto' ? 'bg-purple-500/20 text-purple-400' :
                  strategy.assetClass === 'Equity' ? 'bg-blue-500/20 text-blue-400' :
                  'bg-amber-500/20 text-amber-400'
                }`}>
                  {strategy.assetClass}
                </span>
              </div>
              <span className={`flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-full ${
                strategy.state === 'running'
                  ? 'bg-success/10 text-success'
                  : 'bg-warning/10 text-warning'
              }`}>
                <span className={`w-1.5 h-1.5 rounded-full ${
                  strategy.state === 'running' ? 'bg-success animate-pulse' : 'bg-warning'
                }`} />
                {strategy.state === 'running' ? 'Running' : 'Paused'}
              </span>
            </div>

            <p className="text-xs text-gray-500 mb-4 line-clamp-2">{strategy.description}</p>

            <div className="grid grid-cols-3 gap-3 pt-3 border-t border-border">
              <div>
                <p className="text-xs text-gray-500">P&L</p>
                <p className={`text-sm font-semibold ${strategy.pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                  {strategy.pnl >= 0 ? '+' : ''}${strategy.pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </p>
              </div>
              <div>
                <p className="text-xs text-gray-500">Win Rate</p>
                <p className="text-sm font-semibold text-gray-100">{strategy.winRate}%</p>
              </div>
              <div>
                <p className="text-xs text-gray-500">Sharpe</p>
                <p className="text-sm font-semibold text-gray-100">{strategy.sharpe.toFixed(2)}</p>
              </div>
            </div>
          </Link>
        ))}
      </div>
    </div>
  )
}
