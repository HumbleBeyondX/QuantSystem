'use client'

import { ReactNode, useEffect, useRef, useState } from 'react'

/**
 * TearSheet — professional backtest analytics grid.
 *
 * Fetches GET `${analyticsUrl}?benchmark=XXX` and renders six panels:
 * equity (rebased 100) vs benchmark, drawdown, rolling Sharpe (63d) with the
 * 1.5 gate line, monthly-return heatmap, daily-return distribution, and a
 * stats table (strategy | benchmark + beta/alpha/correlation strip).
 * All charts are inline SVG — no chart library dependencies.
 */

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface SideStats {
  cagr: number
  ann_vol: number
  sharpe: number
  sortino: number
  max_drawdown: number
  calmar: number
  win_rate_days: number
  best_day: number
  worst_day: number
  n_days: number
}

interface RelativeStats {
  beta: number
  alpha_ann: number
  correlation: number
  n_overlap_days: number
}

interface SeriesPoint {
  date: string
  strategy: number
  benchmark?: number | null
}

interface AnalyticsData {
  error?: string
  ann_factor?: number
  benchmark?: string | null
  period?: { start?: string; end?: string }
  equity?: SeriesPoint[]
  drawdown?: SeriesPoint[]
  rolling_sharpe?: Array<{ date: string; sharpe: number }>
  monthly_returns?: Array<{ year: number; months: Array<number | null>; total: number | null }>
  distribution?: { bin_edges: number[]; counts: number[] }
  stats?: { strategy?: SideStats; benchmark?: SideStats; relative?: RelativeStats }
}

// ---------------------------------------------------------------------------
// Shared chart helpers
// ---------------------------------------------------------------------------

const C = {
  strategy: '#10b981',
  benchmark: '#9ca3af',
  rose: '#f43f5e',
  accent: '#8b5cf6',
  gate: '#f59e0b',
  grid: '#1f2937',
  label: '#6b7280',
}

const W = 320
const H = 132
const PLOT_H = H - 16 // bottom strip reserved for date labels

const pct = (v: number, d = 1) => `${(v * 100).toFixed(d)}%`

function downsample<T>(arr: T[], max = 400): T[] {
  if (arr.length <= max) return arr
  const out: T[] = []
  const step = arr.length / max
  for (let i = 0; i < max; i++) out.push(arr[Math.floor(i * step)])
  out.push(arr[arr.length - 1])
  return out
}

// Build an SVG path, lifting the pen across null gaps
function linePath(vals: Array<number | null>, x: (i: number) => number, y: (v: number) => number): string {
  let d = ''
  let pen = false
  for (let i = 0; i < vals.length; i++) {
    const v = vals[i]
    if (v == null || !isFinite(v)) {
      pen = false
      continue
    }
    d += `${pen ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `
    pen = true
  }
  return d.trim()
}

function DateAxis({ dates }: { dates: string[] }) {
  if (dates.length === 0) return null
  const first = dates[0]
  const mid = dates[Math.floor(dates.length / 2)]
  const last = dates[dates.length - 1]
  return (
    <>
      <text x={0} y={H - 3} fill={C.label} fontSize={8}>{first}</text>
      <text x={W / 2} y={H - 3} fill={C.label} fontSize={8} textAnchor="middle">{mid}</text>
      <text x={W} y={H - 3} fill={C.label} fontSize={8} textAnchor="end">{last}</text>
    </>
  )
}

function LegendSwatch({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5 text-[10px] text-gray-400">
      <span className="inline-block w-3 h-0.5 rounded" style={{ backgroundColor: color }} />
      {label}
    </span>
  )
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="bg-background rounded-lg border border-border/50 p-3">
      <p className="text-xs uppercase tracking-wider text-gray-500 mb-2">{title}</p>
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Panel 1 — Equity (rebased 100)
// ---------------------------------------------------------------------------

function EquityPanel({ rows, benchName }: { rows: SeriesPoint[]; benchName: string | null }) {
  const data = downsample(rows)
  const stratVals = data.map(d => d.strategy)
  const benchVals = data.map(d => (typeof d.benchmark === 'number' ? d.benchmark : null))
  const hasBench = benchVals.some(v => v != null)
  const all = stratVals.concat(benchVals.filter((v): v is number => v != null))
  let lo = Math.min(...all)
  let hi = Math.max(...all)
  const pad = (hi - lo || 1) * 0.04
  lo -= pad
  hi += pad
  const x = (i: number) => (i / Math.max(data.length - 1, 1)) * W
  const y = (v: number) => PLOT_H - ((v - lo) / (hi - lo || 1)) * PLOT_H
  const ticks = [lo, (lo + hi) / 2, hi]

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={0} y1={y(t)} x2={W} y2={y(t)} stroke={C.grid} strokeWidth={0.5} />
            <text x={2} y={Math.max(y(t) - 2, 7)} fill={C.label} fontSize={7} className="tabular-nums">
              {t.toFixed(0)}
            </text>
          </g>
        ))}
        {hasBench && (
          <path d={linePath(benchVals, x, y)} fill="none" stroke={C.benchmark} strokeWidth={1.5} opacity={0.8} />
        )}
        <path d={linePath(stratVals, x, y)} fill="none" stroke={C.strategy} strokeWidth={2} />
        <DateAxis dates={data.map(d => d.date)} />
      </svg>
      <div className="flex items-center gap-4 mt-1.5">
        <LegendSwatch color={C.strategy} label="strategy" />
        {hasBench && <LegendSwatch color={C.benchmark} label={benchName || 'benchmark'} />}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Panel 2 — Drawdown
// ---------------------------------------------------------------------------

function DrawdownPanel({ rows, benchName }: { rows: SeriesPoint[]; benchName: string | null }) {
  const data = downsample(rows)
  const stratVals = data.map(d => d.strategy)
  const benchVals = data.map(d => (typeof d.benchmark === 'number' ? d.benchmark : null))
  const hasBench = benchVals.some(v => v != null)
  const all = stratVals.concat(benchVals.filter((v): v is number => v != null))
  const minV = Math.min(...all, -0.001)
  const lo = minV * 1.08
  const x = (i: number) => (i / Math.max(data.length - 1, 1)) * W
  const y = (v: number) => (v / lo) * PLOT_H // 0 at top, lo at bottom (both negative)
  const stratLine = linePath(stratVals, x, y)
  const areaD = `${stratLine} L${W.toFixed(2)},0 L0,0 Z`
  const midTick = lo / 2

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
        <line x1={0} y1={0} x2={W} y2={0} stroke={C.grid} strokeWidth={0.5} />
        <line x1={0} y1={y(midTick)} x2={W} y2={y(midTick)} stroke={C.grid} strokeWidth={0.5} />
        <text x={2} y={8} fill={C.label} fontSize={7} className="tabular-nums">0%</text>
        <text x={2} y={y(midTick) - 2} fill={C.label} fontSize={7} className="tabular-nums">{pct(midTick, 0)}</text>
        <text x={2} y={PLOT_H - 2} fill={C.label} fontSize={7} className="tabular-nums">{pct(lo, 0)}</text>
        <path d={areaD} fill={C.rose} fillOpacity={0.3} />
        <path d={stratLine} fill="none" stroke={C.rose} strokeWidth={1.5} />
        {hasBench && (
          <path d={linePath(benchVals, x, y)} fill="none" stroke={C.benchmark} strokeWidth={1} opacity={0.8} />
        )}
        <DateAxis dates={data.map(d => d.date)} />
      </svg>
      <div className="flex items-center gap-4 mt-1.5">
        <LegendSwatch color={C.rose} label="strategy" />
        {hasBench && <LegendSwatch color={C.benchmark} label={benchName || 'benchmark'} />}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Panel 3 — Rolling Sharpe (63d) with 1.5 gate line
// ---------------------------------------------------------------------------

function RollingSharpePanel({ rows }: { rows: Array<{ date: string; sharpe: number }> }) {
  const data = downsample(rows)
  const vals = data.map(d => d.sharpe)
  let lo = Math.min(...vals, 0)
  let hi = Math.max(...vals, 1.8)
  const pad = (hi - lo || 1) * 0.05
  lo -= pad
  hi += pad
  const x = (i: number) => (i / Math.max(data.length - 1, 1)) * W
  const y = (v: number) => PLOT_H - ((v - lo) / (hi - lo || 1)) * PLOT_H

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1={0} y1={y(0)} x2={W} y2={y(0)} stroke={C.grid} strokeWidth={0.5} />
      <text x={2} y={y(0) - 2} fill={C.label} fontSize={7} className="tabular-nums">0</text>
      {/* 1.5 gate — this platform's validation rule */}
      <line x1={0} y1={y(1.5)} x2={W} y2={y(1.5)} stroke={C.gate} strokeWidth={1} strokeDasharray="4 3" opacity={0.8} />
      <text x={W - 2} y={Math.max(y(1.5) - 3, 7)} fill={C.gate} fontSize={7} textAnchor="end">gate 1.5</text>
      <path d={linePath(vals, x, y)} fill="none" stroke={C.accent} strokeWidth={1.5} />
      <DateAxis dates={data.map(d => d.date)} />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Panel 4 — Monthly returns heatmap
// ---------------------------------------------------------------------------

const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

function heatStyle(v: number | null) {
  if (v == null || !isFinite(v)) return undefined
  const alpha = Math.min(Math.abs(v) * 8, 0.9)
  return {
    backgroundColor: v >= 0 ? `rgba(16,185,129,${alpha.toFixed(3)})` : `rgba(244,63,94,${alpha.toFixed(3)})`,
  }
}

function MonthlyHeatmapPanel({ rows }: { rows: Array<{ year: number; months: Array<number | null>; total: number | null }> }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-[10px] tabular-nums border-separate" style={{ borderSpacing: 1 }}>
        <thead>
          <tr className="text-gray-500">
            <th className="text-left font-normal pr-1" />
            {MONTH_LABELS.map(mo => (
              <th key={mo} className="font-normal text-center px-0.5">{mo}</th>
            ))}
            <th className="font-medium text-center px-1">Total</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.year}>
              <td className="text-gray-400 pr-1">{r.year}</td>
              {r.months.map((v, i) => (
                <td key={i} className="text-center text-gray-100 rounded px-0.5 py-0.5" style={heatStyle(v)}>
                  {v == null ? '' : (v * 100).toFixed(1)}
                </td>
              ))}
              <td className="text-center font-semibold text-gray-100 rounded px-1 py-0.5" style={heatStyle(r.total)}>
                {r.total == null ? '' : (r.total * 100).toFixed(1)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-[9px] text-gray-600 mt-1">values in %</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Panel 5 — Daily return distribution
// ---------------------------------------------------------------------------

function DistributionPanel({ dist }: { dist: { bin_edges: number[]; counts: number[] } }) {
  const edges = dist.bin_edges
  const counts = dist.counts
  if (edges.length < 2 || counts.length === 0) {
    return <p className="text-xs text-gray-500 py-4 text-center">No distribution data</p>
  }
  const xmin = edges[0]
  const xmax = edges[edges.length - 1]
  const span = xmax - xmin || 1
  const x = (v: number) => ((v - xmin) / span) * W
  const maxC = Math.max(...counts, 1)
  const zeroInRange = xmin < 0 && xmax > 0

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <line x1={0} y1={PLOT_H} x2={W} y2={PLOT_H} stroke={C.grid} strokeWidth={0.5} />
      {counts.map((c, i) => {
        const e0 = edges[i]
        const e1 = edges[i + 1] ?? xmax
        const x0 = x(e0)
        const bw = Math.max(x(e1) - x0 - 0.6, 0.4)
        const bh = (c / maxC) * (PLOT_H - 6)
        const pos = (e0 + e1) / 2 >= 0
        return (
          <rect
            key={i}
            x={x0 + 0.3}
            y={PLOT_H - bh}
            width={bw}
            height={bh}
            fill={pos ? C.strategy : C.rose}
            fillOpacity={0.75}
          />
        )
      })}
      {zeroInRange && <line x1={x(0)} y1={0} x2={x(0)} y2={PLOT_H} stroke={C.label} strokeWidth={0.5} strokeDasharray="3 3" />}
      <text x={0} y={H - 3} fill={C.label} fontSize={8} className="tabular-nums">{pct(xmin, 1)}</text>
      {zeroInRange && (
        <text
          x={Math.min(Math.max(x(0), 14), W - 14)}
          y={H - 3}
          fill={C.label}
          fontSize={8}
          textAnchor="middle"
          className="tabular-nums"
        >
          0%
        </text>
      )}
      <text x={W} y={H - 3} fill={C.label} fontSize={8} textAnchor="end" className="tabular-nums">{pct(xmax, 1)}</text>
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Panel 6 — Stats table + relative strip
// ---------------------------------------------------------------------------

const STAT_ROWS: Array<{ key: keyof SideStats; label: string; fmt: (v: number) => string }> = [
  { key: 'cagr', label: 'CAGR', fmt: v => pct(v) },
  { key: 'ann_vol', label: 'Ann Vol', fmt: v => pct(v) },
  { key: 'sharpe', label: 'Sharpe', fmt: v => v.toFixed(2) },
  { key: 'sortino', label: 'Sortino', fmt: v => v.toFixed(2) },
  { key: 'max_drawdown', label: 'Max DD', fmt: v => pct(v) },
  { key: 'calmar', label: 'Calmar', fmt: v => v.toFixed(2) },
  { key: 'win_rate_days', label: 'Daily Win %', fmt: v => pct(v) },
  { key: 'best_day', label: 'Best Day', fmt: v => pct(v, 2) },
  { key: 'worst_day', label: 'Worst Day', fmt: v => pct(v, 2) },
  { key: 'n_days', label: 'N Days', fmt: v => v.toFixed(0) },
]

function StatsPanel({
  stats,
  benchName,
}: {
  stats: { strategy?: SideStats; benchmark?: SideStats; relative?: RelativeStats }
  benchName: string | null
}) {
  const s = stats.strategy
  const b = stats.benchmark
  const rel = stats.relative
  if (!s) return <p className="text-xs text-gray-500 py-4 text-center">No stats</p>

  return (
    <div>
      <table className="w-full text-xs tabular-nums">
        <thead>
          <tr className="text-gray-500 border-b border-border">
            <th className="text-left pb-1.5 font-normal" />
            <th className="text-right pb-1.5 font-medium">Strategy</th>
            {b && <th className="text-right pb-1.5 font-medium">{benchName || 'Benchmark'}</th>}
          </tr>
        </thead>
        <tbody>
          {STAT_ROWS.map(row => {
            const sv = s[row.key]
            const bv = b ? b[row.key] : null
            return (
              <tr key={row.key} className="border-b border-border/30">
                <td className="py-1 text-gray-500">{row.label}</td>
                <td className={`py-1 text-right font-medium ${
                  row.key === 'max_drawdown' || row.key === 'worst_day' ? 'text-danger' : 'text-gray-100'
                }`}>
                  {typeof sv === 'number' ? row.fmt(sv) : '—'}
                </td>
                {b && (
                  <td className="py-1 text-right text-gray-400">
                    {typeof bv === 'number' ? row.fmt(bv) : '—'}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </table>
      {rel && (
        <div className="mt-2 pt-2 border-t border-border/50 flex items-center gap-4 flex-wrap text-[11px] tabular-nums">
          <span className="text-gray-500 uppercase tracking-wider text-[9px]">vs {benchName || 'benchmark'}</span>
          <span className="text-gray-400">Beta <span className="text-gray-100 font-medium">{rel.beta.toFixed(2)}</span></span>
          <span className="text-gray-400">Ann Alpha <span className={`font-medium ${rel.alpha_ann >= 0 ? 'text-success' : 'text-danger'}`}>{pct(rel.alpha_ann)}</span></span>
          <span className="text-gray-400">Corr <span className="text-gray-100 font-medium">{rel.correlation.toFixed(2)}</span></span>
          <span className="text-gray-600">{rel.n_overlap_days}d overlap</span>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// TearSheet — main component
// ---------------------------------------------------------------------------

export default function TearSheet({ analyticsUrl }: { analyticsUrl: string }) {
  const [benchmarks, setBenchmarks] = useState<string[]>([])
  const [benchmark, setBenchmark] = useState('') // '' = None
  const [data, setData] = useState<AnalyticsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [fetchError, setFetchError] = useState('')
  const cache = useRef<Record<string, AnalyticsData>>({})

  // Available benchmarks (fetched once)
  useEffect(() => {
    let alive = true
    ;(async () => {
      try {
        const res = await fetch(`${API}/api/backtest/benchmarks`)
        if (!res.ok) return
        const d = await res.json()
        if (alive && Array.isArray(d?.benchmarks)) setBenchmarks(d.benchmarks)
      } catch {}
    })()
    return () => {
      alive = false
    }
  }, [])

  // Analytics — cached per (url, benchmark)
  useEffect(() => {
    const key = `${analyticsUrl}|${benchmark}`
    const cached = cache.current[key]
    if (cached) {
      setData(cached)
      setLoading(false)
      setFetchError('')
      return
    }
    let alive = true
    setLoading(true)
    setFetchError('')
    ;(async () => {
      try {
        const url = `${API}${analyticsUrl}${benchmark ? `?benchmark=${encodeURIComponent(benchmark)}` : ''}`
        const res = await fetch(url)
        if (!res.ok) {
          if (alive) {
            setData(null)
            setFetchError(`Analytics unavailable (HTTP ${res.status})`)
            setLoading(false)
          }
          return
        }
        const d: AnalyticsData = await res.json()
        cache.current[key] = d
        if (alive) {
          setData(d)
          setLoading(false)
        }
      } catch (e: any) {
        if (alive) {
          setData(null)
          setFetchError(e?.message || 'Failed to load analytics')
          setLoading(false)
        }
      }
    })()
    return () => {
      alive = false
    }
  }, [analyticsUrl, benchmark])

  const benchName = data?.benchmark || (benchmark || null)

  return (
    <div className="space-y-3">
      {/* Header: title + period + benchmark selector */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <p className="text-xs uppercase tracking-wider font-semibold text-gray-400">Tear Sheet</p>
          {data?.period?.start && (
            <span className="text-[11px] text-gray-500 tabular-nums">
              {data.period.start} → {data.period.end}
              {data.ann_factor ? ` · ann ${data.ann_factor}` : ''}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-[10px] text-gray-500">Benchmark:</span>
          {['', ...benchmarks].map(b => (
            <button
              key={b || '__none'}
              onClick={() => setBenchmark(b)}
              className={`px-2 py-0.5 rounded text-[10px] border transition-colors ${
                benchmark === b
                  ? 'border-accent text-accent bg-accent/10'
                  : 'border-border text-gray-500 hover:text-gray-300'
              }`}
            >
              {b || 'None'}
            </button>
          ))}
        </div>
      </div>

      {/* Body */}
      {loading ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {[0, 1, 2, 3, 4, 5].map(i => (
            <div key={i} className="h-44 bg-background border border-border/50 rounded-lg animate-pulse" />
          ))}
        </div>
      ) : fetchError ? (
        <p className="text-xs text-gray-500 py-4 text-center">{fetchError}</p>
      ) : data?.error ? (
        <p className="text-xs text-gray-500 py-4 text-center">{data.error}</p>
      ) : data ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {data.equity && data.equity.length > 1 && (
            <Panel title="Equity (rebased 100)">
              <EquityPanel rows={data.equity} benchName={benchName} />
            </Panel>
          )}
          {data.drawdown && data.drawdown.length > 1 && (
            <Panel title="Drawdown">
              <DrawdownPanel rows={data.drawdown} benchName={benchName} />
            </Panel>
          )}
          {data.rolling_sharpe && data.rolling_sharpe.length > 1 && (
            <Panel title="Rolling Sharpe (63d)">
              <RollingSharpePanel rows={data.rolling_sharpe} />
            </Panel>
          )}
          {data.monthly_returns && data.monthly_returns.length > 0 && (
            <Panel title="Monthly Returns">
              <MonthlyHeatmapPanel rows={data.monthly_returns} />
            </Panel>
          )}
          {data.distribution && (
            <Panel title="Daily Return Distribution">
              <DistributionPanel dist={data.distribution} />
            </Panel>
          )}
          {data.stats?.strategy && (
            <Panel title="Stats">
              <StatsPanel stats={data.stats} benchName={benchName} />
            </Panel>
          )}
        </div>
      ) : null}
    </div>
  )
}
