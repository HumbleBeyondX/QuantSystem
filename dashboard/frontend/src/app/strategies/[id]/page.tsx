'use client'

import Link from 'next/link'
import { useState } from 'react'
import { useParams } from 'next/navigation'

/**
 * Strategy Detail Page
 *
 * Shows performance metrics, signals history, equity curve,
 * and a pause/resume toggle for a specific strategy.
 */

const strategyData: Record<string, {
  name: string
  assetClass: string
  state: 'running' | 'paused'
  pnl: number
  sharpe: number
  winRate: number
  totalTrades: number
  maxDrawdown: number
  avgHoldTime: string
  description: string
}> = {
  'momentum-btc': {
    name: 'BTC Momentum',
    assetClass: 'Crypto',
    state: 'running',
    pnl: 12_450.30,
    sharpe: 1.85,
    winRate: 62.5,
    totalTrades: 148,
    maxDrawdown: -8.2,
    avgHoldTime: '4h 23m',
    description: 'Trend-following momentum strategy on BTC/USD with adaptive lookback period and volatility-adjusted position sizing.',
  },
  'mean-rev-eth': {
    name: 'ETH Mean Reversion',
    assetClass: 'Crypto',
    state: 'running',
    pnl: 5_230.00,
    sharpe: 1.42,
    winRate: 58.3,
    totalTrades: 210,
    maxDrawdown: -12.1,
    avgHoldTime: '2h 15m',
    description: 'Statistical mean reversion on ETH using Bollinger Bands and RSI with dynamic band width.',
  },
  'pairs-tech': {
    name: 'Tech Pairs Trading',
    assetClass: 'Equity',
    state: 'paused',
    pnl: -1_820.50,
    sharpe: 0.65,
    winRate: 45.0,
    totalTrades: 92,
    maxDrawdown: -15.4,
    avgHoldTime: '1d 6h',
    description: 'Cointegrated pairs trading on FAANG stocks using Johansen cointegration test.',
  },
  'spy-gamma-scalp': {
    name: 'SPY Gamma Scalp',
    assetClass: 'Options',
    state: 'running',
    pnl: 8_920.75,
    sharpe: 2.10,
    winRate: 71.2,
    totalTrades: 325,
    maxDrawdown: -4.8,
    avgHoldTime: '45m',
    description: 'Gamma scalping strategy on SPY 0DTE options with real-time Greeks hedging.',
  },
  'vol-arb': {
    name: 'Volatility Arb',
    assetClass: 'Options',
    state: 'paused',
    pnl: 3_100.00,
    sharpe: 1.20,
    winRate: 55.8,
    totalTrades: 67,
    maxDrawdown: -9.6,
    avgHoldTime: '3d 2h',
    description: 'Implied vs realized volatility arbitrage across index options using variance swaps.',
  },
  'sector-rotation': {
    name: 'Sector Rotation',
    assetClass: 'Equity',
    state: 'running',
    pnl: 6_780.25,
    sharpe: 1.55,
    winRate: 60.0,
    totalTrades: 44,
    maxDrawdown: -7.3,
    avgHoldTime: '22d',
    description: 'Monthly sector rotation based on relative strength and macro regime signals.',
  },
}

const signals = [
  { timestamp: '2026-03-14 09:32:15', symbol: 'BTC-USD', direction: 'LONG', strength: 0.87 },
  { timestamp: '2026-03-14 08:15:42', symbol: 'BTC-USD', direction: 'CLOSE', strength: 0.92 },
  { timestamp: '2026-03-13 22:10:05', symbol: 'BTC-USD', direction: 'SHORT', strength: 0.65 },
  { timestamp: '2026-03-13 18:45:30', symbol: 'BTC-USD', direction: 'LONG', strength: 0.78 },
  { timestamp: '2026-03-13 14:22:18', symbol: 'BTC-USD', direction: 'CLOSE', strength: 0.81 },
  { timestamp: '2026-03-13 10:05:44', symbol: 'ETH-USD', direction: 'LONG', strength: 0.72 },
  { timestamp: '2026-03-12 23:30:12', symbol: 'BTC-USD', direction: 'SHORT', strength: 0.59 },
  { timestamp: '2026-03-12 16:48:55', symbol: 'BTC-USD', direction: 'LONG', strength: 0.91 },
]

export default function StrategyDetailPage() {
  const params = useParams()
  const id = params.id as string
  const strategy = strategyData[id]
  const [isRunning, setIsRunning] = useState(strategy?.state === 'running')

  if (!strategy) {
    return (
      <div className="space-y-4">
        <Link href="/strategies" className="text-accent hover:underline text-sm">&larr; Back to Strategies</Link>
        <div className="bg-surface rounded-xl border border-border p-12 text-center">
          <p className="text-gray-400">Strategy &quot;{id}&quot; not found.</p>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <Link href="/strategies" className="text-accent hover:underline text-sm">&larr; Back to Strategies</Link>
          <h2 className="text-2xl font-bold mt-2">{strategy.name}</h2>
          <div className="flex items-center gap-3 mt-1">
            <span className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
              strategy.assetClass === 'Crypto' ? 'bg-purple-500/20 text-purple-400' :
              strategy.assetClass === 'Equity' ? 'bg-blue-500/20 text-blue-400' :
              'bg-amber-500/20 text-amber-400'
            }`}>
              {strategy.assetClass}
            </span>
            <span className={`flex items-center gap-1.5 text-xs font-medium ${
              isRunning ? 'text-success' : 'text-warning'
            }`}>
              <span className={`w-1.5 h-1.5 rounded-full ${isRunning ? 'bg-success animate-pulse' : 'bg-warning'}`} />
              {isRunning ? 'Running' : 'Paused'}
            </span>
          </div>
          <p className="text-sm text-gray-500 mt-2 max-w-2xl">{strategy.description}</p>
        </div>
        <button
          onClick={() => setIsRunning(!isRunning)}
          className={`px-5 py-2.5 rounded-lg text-sm font-medium transition-colors ${
            isRunning
              ? 'bg-warning/10 text-warning border border-warning/30 hover:bg-warning/20'
              : 'bg-success/10 text-success border border-success/30 hover:bg-success/20'
          }`}
        >
          {isRunning ? 'Pause Strategy' : 'Resume Strategy'}
        </button>
      </div>

      {/* Performance Metrics Row */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        <MetricCard
          title="P&L"
          value={`${strategy.pnl >= 0 ? '+' : ''}$${strategy.pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}`}
          color={strategy.pnl >= 0 ? 'text-success' : 'text-danger'}
        />
        <MetricCard title="Sharpe Ratio" value={strategy.sharpe.toFixed(2)} />
        <MetricCard title="Win Rate" value={`${strategy.winRate}%`} />
        <MetricCard title="Total Trades" value={strategy.totalTrades.toString()} />
        <MetricCard
          title="Max Drawdown"
          value={`${strategy.maxDrawdown}%`}
          color="text-danger"
        />
        <MetricCard title="Avg Hold Time" value={strategy.avgHoldTime} />
      </div>

      {/* Equity Curve */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-4">Equity Curve</h3>
        <div className="h-64 flex items-center justify-center relative">
          {/* Placeholder chart visualization */}
          <svg className="w-full h-full" viewBox="0 0 800 200" preserveAspectRatio="none">
            <defs>
              <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3b82f6" stopOpacity="0.3" />
                <stop offset="100%" stopColor="#3b82f6" stopOpacity="0" />
              </linearGradient>
            </defs>
            {/* Grid lines */}
            {[0, 1, 2, 3, 4].map(i => (
              <line key={i} x1="0" y1={i * 50} x2="800" y2={i * 50} stroke="#1f2937" strokeWidth="1" />
            ))}
            {/* Area fill */}
            <path
              d="M0,180 L80,160 L160,140 L240,120 L320,130 L400,100 L480,80 L560,90 L640,60 L720,40 L800,30 L800,200 L0,200 Z"
              fill="url(#equityGradient)"
            />
            {/* Line */}
            <path
              d="M0,180 L80,160 L160,140 L240,120 L320,130 L400,100 L480,80 L560,90 L640,60 L720,40 L800,30"
              fill="none"
              stroke="#3b82f6"
              strokeWidth="2"
            />
          </svg>
        </div>
      </div>

      {/* Signals History */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-4">Signals History</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-400 border-b border-border">
                <th className="text-left pb-3 font-medium">Timestamp</th>
                <th className="text-left pb-3 font-medium">Symbol</th>
                <th className="text-left pb-3 font-medium">Direction</th>
                <th className="text-right pb-3 font-medium">Strength</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((sig, i) => (
                <tr key={i} className="border-b border-border/50 hover:bg-background/50 transition-colors">
                  <td className="py-3 text-gray-400 font-mono text-xs">{sig.timestamp}</td>
                  <td className="py-3 text-gray-100 font-medium">{sig.symbol}</td>
                  <td className="py-3">
                    <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                      sig.direction === 'LONG' ? 'bg-success/10 text-success' :
                      sig.direction === 'SHORT' ? 'bg-danger/10 text-danger' :
                      'bg-gray-500/10 text-gray-400'
                    }`}>
                      {sig.direction}
                    </span>
                  </td>
                  <td className="py-3 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <div className="w-16 h-1.5 bg-background rounded-full overflow-hidden">
                        <div
                          className="h-full bg-accent rounded-full"
                          style={{ width: `${sig.strength * 100}%` }}
                        />
                      </div>
                      <span className="text-gray-400 text-xs w-8 text-right">{(sig.strength * 100).toFixed(0)}%</span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

function MetricCard({ title, value, color = 'text-gray-100' }: { title: string; value: string; color?: string }) {
  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <p className="text-xs text-gray-500">{title}</p>
      <p className={`text-lg font-bold mt-1 ${color}`}>{value}</p>
    </div>
  )
}
