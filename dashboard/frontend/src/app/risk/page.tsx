'use client'

/**
 * Risk Dashboard Page
 *
 * Displays risk metrics (VaR, CVaR, Drawdown, Leverage),
 * Greeks exposure, circuit breaker status, and correlation matrix.
 */

const riskMetrics = [
  { title: 'Value at Risk (95%)', value: '$12,340', subtitle: '1-day VaR', color: 'text-danger' },
  { title: 'CVaR (95%)', value: '$18,520', subtitle: 'Expected Shortfall', color: 'text-danger' },
  { title: 'Current Drawdown', value: '-3.2%', subtitle: 'From peak equity', color: 'text-warning' },
  { title: 'Max Drawdown', value: '-12.4%', subtitle: '90-day rolling', color: 'text-danger' },
  { title: 'Gross Leverage', value: '1.85x', subtitle: 'Target: 2.0x', color: 'text-gray-100' },
  { title: 'Net Exposure', value: '0.72', subtitle: 'Long-biased', color: 'text-accent' },
]

const greeks = [
  { name: 'Delta', value: 0.35, range: [-1, 1], unit: '', color: '#3b82f6' },
  { name: 'Gamma', value: 0.08, range: [0, 0.2], unit: '', color: '#10b981' },
  { name: 'Theta', value: -245.50, range: [-500, 0], unit: '$/day', color: '#ef4444' },
  { name: 'Vega', value: 1820.00, range: [0, 5000], unit: '$/1%', color: '#a855f7' },
]

const circuitBreakers = [
  { name: 'Max Daily Loss', threshold: '$25,000', current: '$3,200', status: 'ok' as const, pct: 12.8 },
  { name: 'Max Position Size', threshold: '15%', current: '8.4%', status: 'ok' as const, pct: 56.0 },
  { name: 'Max Drawdown', threshold: '20%', current: '12.4%', status: 'warning' as const, pct: 62.0 },
  { name: 'Max Leverage', threshold: '3.0x', current: '1.85x', status: 'ok' as const, pct: 61.7 },
  { name: 'Correlation Limit', threshold: '0.80', current: '0.65', status: 'ok' as const, pct: 81.3 },
]

const correlationAssets = ['BTC', 'ETH', 'SPY', 'AAPL', 'TSLA', 'GLD']
const correlationMatrix = [
  [1.00, 0.85, 0.32, 0.28, 0.35, -0.12],
  [0.85, 1.00, 0.28, 0.25, 0.30, -0.08],
  [0.32, 0.28, 1.00, 0.78, 0.65, 0.15],
  [0.28, 0.25, 0.78, 1.00, 0.52, 0.10],
  [0.35, 0.30, 0.65, 0.52, 1.00, 0.05],
  [-0.12, -0.08, 0.15, 0.10, 0.05, 1.00],
]

function getCorrelationColor(val: number): string {
  if (val >= 0.7) return 'bg-danger/60'
  if (val >= 0.4) return 'bg-warning/40'
  if (val >= 0) return 'bg-success/20'
  return 'bg-blue-500/30'
}

export default function RiskPage() {
  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Risk Dashboard</h2>

      {/* Risk Metric Cards */}
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
        {/* Greeks Exposure */}
        <div className="bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">Greeks Exposure</h3>
          <div className="space-y-5">
            {greeks.map((greek) => {
              const rangeSpan = greek.range[1] - greek.range[0]
              const normalizedPct = ((greek.value - greek.range[0]) / rangeSpan) * 100
              const clampedPct = Math.max(0, Math.min(100, normalizedPct))
              return (
                <div key={greek.name}>
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-sm font-medium text-gray-100">{greek.name}</span>
                    <span className="text-sm text-gray-400">
                      {greek.value.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                      {greek.unit && <span className="text-xs text-gray-500 ml-1">{greek.unit}</span>}
                    </span>
                  </div>
                  {/* Gauge bar */}
                  <div className="relative h-3 bg-background rounded-full overflow-hidden">
                    <div
                      className="absolute top-0 left-0 h-full rounded-full transition-all"
                      style={{ width: `${clampedPct}%`, backgroundColor: greek.color }}
                    />
                    {/* Center marker for delta */}
                    {greek.range[0] < 0 && (
                      <div
                        className="absolute top-0 h-full w-0.5 bg-gray-500"
                        style={{ left: `${((0 - greek.range[0]) / rangeSpan) * 100}%` }}
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
        </div>

        {/* Circuit Breaker Status */}
        <div className="bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">Circuit Breakers</h3>
          <div className="space-y-4">
            {circuitBreakers.map((cb) => (
              <div key={cb.name} className="flex items-center gap-3">
                {/* Status indicator */}
                <div className={`w-2.5 h-2.5 rounded-full flex-shrink-0 ${
                  cb.status === 'ok' ? 'bg-success' :
                  cb.status === 'warning' ? 'bg-warning animate-pulse' :
                  'bg-danger animate-pulse'
                }`} />
                <div className="flex-1 min-w-0">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-sm text-gray-100">{cb.name}</span>
                    <span className="text-xs text-gray-400">
                      {cb.current} <span className="text-gray-600">/ {cb.threshold}</span>
                    </span>
                  </div>
                  <div className="h-1.5 bg-background rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full transition-all ${
                        cb.pct < 50 ? 'bg-success' :
                        cb.pct < 75 ? 'bg-warning' :
                        'bg-danger'
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
              <div className="w-2.5 h-2.5 rounded-full bg-success" />
              <span className="text-xs text-gray-400">All circuit breakers operational</span>
            </div>
          </div>
        </div>
      </div>

      {/* Correlation Matrix */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-4">Correlation Matrix</h3>
        <div className="overflow-x-auto">
          <table className="text-sm mx-auto">
            <thead>
              <tr>
                <th className="p-2"></th>
                {correlationAssets.map((asset) => (
                  <th key={asset} className="p-2 text-center text-gray-400 font-medium text-xs">{asset}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {correlationAssets.map((asset, i) => (
                <tr key={asset}>
                  <td className="p-2 text-gray-400 font-medium text-xs">{asset}</td>
                  {correlationMatrix[i].map((val, j) => (
                    <td key={j} className="p-1">
                      <div className={`w-14 h-10 rounded flex items-center justify-center text-xs font-mono ${
                        i === j ? 'bg-gray-700/50 text-gray-500' : `${getCorrelationColor(val)} text-gray-100`
                      }`}>
                        {val.toFixed(2)}
                      </div>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex items-center justify-center gap-4 mt-4 text-[10px] text-gray-500">
          <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-blue-500/30" /> Negative</span>
          <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-success/20" /> Low</span>
          <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-warning/40" /> Medium</span>
          <span className="flex items-center gap-1"><span className="w-3 h-3 rounded bg-danger/60" /> High</span>
        </div>
      </div>
    </div>
  )
}
