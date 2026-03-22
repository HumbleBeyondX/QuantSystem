'use client'

import { useMemo } from 'react'

/**
 * Dashboard Overview — real-time trading dashboard overview.
 *
 * Displays: KPI cards, equity curve, strategy allocation,
 * top performers, live options activity feed.
 */

// ---------------------------------------------------------------------------
// Mock data (replaced by API in production)
// ---------------------------------------------------------------------------

// Seeded PRNG to avoid SSR/client hydration mismatch
function seededRandom(seed: number) {
  let s = seed
  return () => {
    s = (s * 16807 + 0) % 2147483647
    return s / 2147483647
  }
}

function generateEquityCurve(days: number, initial: number) {
  const rng = seededRandom(42)
  const points: { date: string; value: number; drawdown: number }[] = []
  let value = initial
  let peak = initial
  for (let i = 0; i < days; i++) {
    const drift = 0.0004 + Math.sin(i / 30) * 0.0002
    const noise = (rng() - 0.48) * 0.012
    value *= 1 + drift + noise
    peak = Math.max(peak, value)
    const dd = (value - peak) / peak
    const d = new Date(2024, 0, 1)
    d.setDate(d.getDate() + i)
    points.push({
      date: d.toISOString().slice(5, 10),
      value: Math.round(value),
      drawdown: Math.round(dd * 10000) / 100,
    })
  }
  return points
}

const equity = generateEquityCurve(180, 100000)
const lastVal = equity[equity.length - 1].value
const totalReturn = ((lastVal - 100000) / 100000 * 100).toFixed(2)
const maxDD = Math.min(...equity.map(e => e.drawdown)).toFixed(2)

const strategies = [
  { name: 'Crypto Scalping', pnl: '+$12,340', winRate: '62.3%', sharpe: '2.41', status: 'running' },
  { name: 'Equity Factor', pnl: '+$8,720', winRate: '58.1%', sharpe: '1.87', status: 'running' },
  { name: 'Options Delta-N', pnl: '+$5,105', winRate: '71.2%', sharpe: '1.52', status: 'running' },
  { name: 'Pairs Trading', pnl: '+$3,210', winRate: '55.0%', sharpe: '1.34', status: 'paused' },
  { name: 'Crypto Swing', pnl: '-$1,420', winRate: '45.8%', sharpe: '0.72', status: 'running' },
]

const optionsActivity = [
  { time: '14:32:05', symbol: 'AAPL 190C', type: 'SWEEP', side: 'BUY', size: '1,200', premium: '$312K', sentiment: 'Bullish' },
  { time: '14:31:42', symbol: 'TSLA 250P', type: 'BLOCK', side: 'SELL', size: '850', premium: '$425K', sentiment: 'Bearish' },
  { time: '14:30:18', symbol: 'SPY 520C', type: 'SWEEP', side: 'BUY', size: '3,400', premium: '$890K', sentiment: 'Bullish' },
  { time: '14:29:55', symbol: 'NVDA 900C', type: 'BLOCK', side: 'BUY', size: '600', premium: '$1.2M', sentiment: 'Bullish' },
  { time: '14:28:30', symbol: 'QQQ 440P', type: 'SWEEP', side: 'BUY', size: '2,100', premium: '$515K', sentiment: 'Bearish' },
]

const topPerformers = [
  { symbol: 'BTC/USDT', strategy: 'Crypto Scalping', pnl: '+$5,280', change: '+2.4%' },
  { symbol: 'AAPL', strategy: 'Equity Factor', pnl: '+$3,140', change: '+1.8%' },
  { symbol: 'ETH/USDT', strategy: 'Crypto Swing', pnl: '+$2,100', change: '+3.1%' },
  { symbol: 'NVDA', strategy: 'Options Delta-N', pnl: '+$1,950', change: '+1.2%' },
  { symbol: 'SPY/IWM', strategy: 'Pairs Trading', pnl: '+$1,320', change: '+0.8%' },
]

// ---------------------------------------------------------------------------
// SVG Equity Curve
// ---------------------------------------------------------------------------

function EquityCurveChart({ data }: { data: typeof equity }) {
  const { pathD, areaD, minY, maxY, width, height } = useMemo(() => {
    const w = 800, h = 200, pad = 10
    const vals = data.map(d => d.value)
    const mn = Math.min(...vals) * 0.999
    const mx = Math.max(...vals) * 1.001
    const xScale = (i: number) => pad + (i / (data.length - 1)) * (w - 2 * pad)
    const yScale = (v: number) => h - pad - ((v - mn) / (mx - mn)) * (h - 2 * pad)

    const pts = data.map((d, i) => `${xScale(i).toFixed(1)},${yScale(d.value).toFixed(1)}`)
    const pathD = 'M' + pts.join('L')
    const areaD = pathD + `L${xScale(data.length - 1).toFixed(1)},${(h - pad).toFixed(1)}L${pad},${(h - pad).toFixed(1)}Z`
    return { pathD, areaD, minY: mn, maxY: mx, width: w, height: h }
  }, [data])

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-full">
      <defs>
        <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#3b82f6" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#3b82f6" stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <path d={areaD} fill="url(#eqGrad)" />
      <path d={pathD} fill="none" stroke="#3b82f6" strokeWidth="2" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function DashboardPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Dashboard Overview</h2>
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-success animate-pulse" />
          <span className="text-sm text-gray-400">Live Trading</span>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <KPICard label="Portfolio Value" value={`$${lastVal.toLocaleString()}`} sub="Initial: $100,000" color="text-gray-100" />
        <KPICard label="Total Return" value={`+${totalReturn}%`} sub="Since Jan 2024" color="text-success" />
        <KPICard label="Max Drawdown" value={`${maxDD}%`} sub="Peak to trough" color="text-danger" />
        <KPICard label="Sharpe Ratio" value="1.87" sub="Annualized" color="text-primary" />
      </div>

      {/* Equity Curve */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">Portfolio Performance</h3>
          <div className="flex gap-2 text-xs">
            {['1W', '1M', '3M', '6M', 'YTD', 'ALL'].map(p => (
              <button key={p} className={`px-2 py-1 rounded ${p === '6M' ? 'bg-primary text-white' : 'text-gray-400 hover:text-gray-200'}`}>{p}</button>
            ))}
          </div>
        </div>
        <div className="h-52">
          <EquityCurveChart data={equity} />
        </div>
      </div>

      {/* Bottom row: 3 columns */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Active Strategies */}
        <div className="bg-surface rounded-xl border border-border p-5">
          <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Active Strategies</h3>
          <div className="space-y-3">
            {strategies.map(s => (
              <div key={s.name} className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className={`w-1.5 h-1.5 rounded-full ${s.status === 'running' ? 'bg-success' : 'bg-warning'}`} />
                  <span className="text-sm">{s.name}</span>
                </div>
                <div className="text-right">
                  <span className={`text-sm font-medium ${s.pnl.startsWith('+') ? 'text-success' : 'text-danger'}`}>{s.pnl}</span>
                  <span className="text-xs text-gray-500 ml-2">SR {s.sharpe}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Top Performers */}
        <div className="bg-surface rounded-xl border border-border p-5">
          <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Top Performers</h3>
          <div className="space-y-3">
            {topPerformers.map(t => (
              <div key={t.symbol} className="flex items-center justify-between">
                <div>
                  <span className="text-sm font-medium">{t.symbol}</span>
                  <span className="text-xs text-gray-500 ml-2">{t.strategy}</span>
                </div>
                <div className="text-right">
                  <span className="text-sm font-medium text-success">{t.pnl}</span>
                  <span className="text-xs text-gray-500 ml-1">{t.change}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Options Activity Feed */}
        <div className="bg-surface rounded-xl border border-border p-5">
          <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-3">Options Flow</h3>
          <div className="space-y-2">
            {optionsActivity.map((a, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <span className="text-gray-500 w-14">{a.time}</span>
                <span className="font-medium w-20 truncate">{a.symbol}</span>
                <span className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
                  a.sentiment === 'Bullish' ? 'bg-success/20 text-success' : 'bg-danger/20 text-danger'
                }`}>{a.type}</span>
                <span className="text-gray-400 ml-auto">{a.premium}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function KPICard({ label, value, sub, color = 'text-gray-100' }: {
  label: string; value: string; sub?: string; color?: string
}) {
  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <p className="text-xs text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-xl font-bold mt-1 ${color}`}>{value}</p>
      {sub && <p className="text-xs text-gray-500 mt-0.5">{sub}</p>}
    </div>
  )
}
