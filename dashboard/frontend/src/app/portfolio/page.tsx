'use client'

import Link from 'next/link'

/**
 * Portfolio Page
 *
 * Displays current positions, asset allocation breakdown,
 * and cash balance overview.
 */

const positions = [
  { symbol: 'AAPL', qty: 150, avgPrice: 178.50, currentPrice: 192.30, sector: 'Technology' },
  { symbol: 'MSFT', qty: 100, avgPrice: 365.20, currentPrice: 412.80, sector: 'Technology' },
  { symbol: 'BTC-USD', qty: 2.5, avgPrice: 42000.00, currentPrice: 67500.00, sector: 'Crypto' },
  { symbol: 'ETH-USD', qty: 25, avgPrice: 2200.00, currentPrice: 3450.00, sector: 'Crypto' },
  { symbol: 'SPY', qty: 200, avgPrice: 448.00, currentPrice: 512.40, sector: 'ETF' },
  { symbol: 'TSLA', qty: 50, avgPrice: 245.00, currentPrice: 218.60, sector: 'Technology' },
  { symbol: 'AMZN', qty: 80, avgPrice: 155.30, currentPrice: 185.70, sector: 'Technology' },
  { symbol: 'GLD', qty: 100, avgPrice: 185.00, currentPrice: 198.50, sector: 'Commodities' },
]

const allocation = [
  { label: 'Technology', pct: 42, color: 'bg-blue-500' },
  { label: 'Crypto', pct: 28, color: 'bg-purple-500' },
  { label: 'ETF', pct: 18, color: 'bg-emerald-500' },
  { label: 'Commodities', pct: 7, color: 'bg-yellow-500' },
  { label: 'Cash', pct: 5, color: 'bg-gray-500' },
]

const cashBalance = 52_340.75

export default function PortfolioPage() {
  const totalValue = positions.reduce((sum, p) => sum + p.qty * p.currentPrice, 0) + cashBalance
  const totalCost = positions.reduce((sum, p) => sum + p.qty * p.avgPrice, 0)
  const totalPnL = totalValue - totalCost - cashBalance
  const totalPnLPct = (totalPnL / totalCost) * 100

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Portfolio</h2>
        <p className="text-sm text-gray-400">
          Total Value: <span className="text-gray-100 font-semibold">${totalValue.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
        </p>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="bg-surface rounded-xl border border-border p-4">
          <p className="text-sm text-gray-400">Total P&L</p>
          <p className={`text-2xl font-bold mt-1 ${totalPnL >= 0 ? 'text-success' : 'text-danger'}`}>
            {totalPnL >= 0 ? '+' : ''}${totalPnL.toLocaleString(undefined, { minimumFractionDigits: 2 })}
          </p>
          <p className={`text-xs mt-1 ${totalPnL >= 0 ? 'text-success' : 'text-danger'}`}>
            {totalPnL >= 0 ? '+' : ''}{totalPnLPct.toFixed(2)}%
          </p>
        </div>
        <div className="bg-surface rounded-xl border border-border p-4">
          <p className="text-sm text-gray-400">Open Positions</p>
          <p className="text-2xl font-bold mt-1 text-gray-100">{positions.length}</p>
          <p className="text-xs text-gray-500 mt-1">Across {new Set(positions.map(p => p.sector)).size} sectors</p>
        </div>
        <div className="bg-surface rounded-xl border border-border p-4">
          <p className="text-sm text-gray-400">Cash Balance</p>
          <p className="text-2xl font-bold mt-1 text-gray-100">
            ${cashBalance.toLocaleString(undefined, { minimumFractionDigits: 2 })}
          </p>
          <p className="text-xs text-gray-500 mt-1">{((cashBalance / totalValue) * 100).toFixed(1)}% of portfolio</p>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Positions Table */}
        <div className="lg:col-span-2 bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">Positions</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-border">
                  <th className="text-left pb-3 font-medium">Symbol</th>
                  <th className="text-right pb-3 font-medium">Qty</th>
                  <th className="text-right pb-3 font-medium">Avg Price</th>
                  <th className="text-right pb-3 font-medium">Current</th>
                  <th className="text-right pb-3 font-medium">P&L</th>
                  <th className="text-right pb-3 font-medium">%</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos) => {
                  const pnl = (pos.currentPrice - pos.avgPrice) * pos.qty
                  const pnlPct = ((pos.currentPrice - pos.avgPrice) / pos.avgPrice) * 100
                  return (
                    <tr key={pos.symbol} className="border-b border-border/50 hover:bg-background/50 transition-colors">
                      <td className="py-3 font-medium text-gray-100">{pos.symbol}</td>
                      <td className="py-3 text-right text-gray-400">{pos.qty}</td>
                      <td className="py-3 text-right text-gray-400">${pos.avgPrice.toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
                      <td className="py-3 text-right text-gray-100">${pos.currentPrice.toLocaleString(undefined, { minimumFractionDigits: 2 })}</td>
                      <td className={`py-3 text-right font-medium ${pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                        {pnl >= 0 ? '+' : ''}${pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                      </td>
                      <td className={`py-3 text-right ${pnl >= 0 ? 'text-success' : 'text-danger'}`}>
                        {pnl >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Asset Allocation */}
        <div className="bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">Asset Allocation</h3>
          {/* Pie Chart Placeholder */}
          <div className="relative w-48 h-48 mx-auto mb-6">
            <svg viewBox="0 0 100 100" className="w-full h-full -rotate-90">
              {(() => {
                let cumulative = 0
                return allocation.map((a, i) => {
                  const dashArray = `${a.pct * 2.83} ${283 - a.pct * 2.83}`
                  const dashOffset = -cumulative * 2.83
                  cumulative += a.pct
                  const colors = ['#3b82f6', '#a855f7', '#10b981', '#f59e0b', '#6b7280']
                  return (
                    <circle
                      key={i}
                      cx="50" cy="50" r="45"
                      fill="none"
                      stroke={colors[i]}
                      strokeWidth="10"
                      strokeDasharray={dashArray}
                      strokeDashoffset={dashOffset}
                    />
                  )
                })
              })()}
            </svg>
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="text-center">
                <p className="text-xs text-gray-400">Total</p>
                <p className="text-sm font-bold text-gray-100">${(totalValue / 1000).toFixed(0)}K</p>
              </div>
            </div>
          </div>
          {/* Legend */}
          <div className="space-y-2">
            {allocation.map((a) => (
              <div key={a.label} className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  <div className={`w-3 h-3 rounded-full ${a.color}`} />
                  <span className="text-gray-400">{a.label}</span>
                </div>
                <span className="text-gray-100 font-medium">{a.pct}%</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
