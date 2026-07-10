'use client'

/**
 * Overview — quant-fund command center. Real data only.
 *
 * Sections (all from live endpoints, graceful degradation, no fake numbers):
 *   1. Header strip     — /api/monitor/strategies rollup (equity, P&L, positions)
 *   2. Pipeline band    — /api/strategies/registry (lifecycle stages + gate rule)
 *   3. Deployed cards   — /api/monitor/strategies (per-strategy cards + sparklines)
 *   4. Validated records — /api/backtest/records (official research registry)
 *   5. Ops strip        — /api/deployments/summary (cloud runners)
 *
 * Card design / sparkline / API shape shared with monitor/page.tsx.
 * Auto-refreshes every 15s.
 */

import { useCallback, useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'
const REFRESH_MS = 15_000

/* ── Types ──────────────────────────────────────────────────── */

interface Card {
  id: string
  name: string
  direction: string
  mode: string
  status: string
  equity: number | null
  today_realized: number | null
  today_pct: number | null
  unrealized: number | null
  total_return_pct: number | null
  win_rate: number | null
  trades: number | null
  profit_factor: number | null
  expectancy: number | null
  positions: number | null
  flags: string[]
  source: string
  updated_at: string | null
  spark: number[] | null
}

interface Snapshot {
  generated_at: string
  total_equity: number
  today_realized: number
  unrealized: number
  open_positions: number
  strategies: Card[]
}

interface Registry {
  stages: string[]
  groups: Record<string, { id: string; name: string }[]>
  gate_rule: string
  counts: Record<string, number>
}

interface BacktestRecord {
  id: string
  name: string
  status: string
  verdict: string
  headline: {
    sharpe?: number
    oos_sharpe?: number
    max_dd?: number
    cagr?: number
    correlation?: number
  }
  script: string
  has_curve: boolean
  rerun: string | null
}

interface RecordsPayload {
  gate_rule: string
  records: BacktestRecord[]
}

interface OpsSummary {
  total_deployments: number
  online: number
  running: number
  total_session_pnl: number
  total_session_trades: number
  total_session_orders?: number
}

/* ── Helpers (shared with monitor page) ─────────────────────── */

const fmt$ = (v: number | null, signed = true) =>
  v == null
    ? '—'
    : `${signed && v >= 0 ? '+' : ''}$${Math.abs(v).toLocaleString('en-US', {
        maximumFractionDigits: 2,
      })}${v < 0 ? '' : ''}`.replace('+$-', '-$')

const pnlColor = (v: number | null) =>
  v == null ? 'text-gray-500' : v >= 0 ? 'text-emerald-400' : 'text-rose-400'

const STATUS_STYLE: Record<string, string> = {
  running: 'bg-emerald-500/15 text-emerald-400',
  flat: 'bg-sky-500/15 text-sky-300',
  stale: 'bg-amber-500/15 text-amber-400',
  unreachable: 'bg-rose-500/15 text-rose-400',
}
const STATUS_LABEL: Record<string, string> = {
  running: 'RUNNING', flat: 'FLAT', stale: 'STALE', unreachable: 'UNREACHABLE',
}

const MODE_STYLE: Record<string, string> = {
  live: 'bg-emerald-500/20 text-emerald-400',
  paper: 'bg-sky-500/20 text-sky-300',
}

/* ── Generic polled-endpoint hook ───────────────────────────── */

function usePolled<T>(path: string, refreshMs: number) {
  const [data, setData] = useState<T | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API}${path}`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setData(await r.json())
      setErr(null)
    } catch (e) {
      setErr(String(e))
    } finally {
      setLoading(false)
    }
  }, [path])

  useEffect(() => {
    load()
    const t = setInterval(load, refreshMs)
    return () => clearInterval(t)
  }, [load, refreshMs])

  return { data, err, loading }
}

/* ── Sparkline (from monitor page) ──────────────────────────── */

function Spark({ data }: { data: number[] | null }) {
  if (!data || data.length < 2)
    return (
      <div className="w-32 h-10 flex items-center justify-center text-[10px] text-gray-600 border border-dashed border-gray-700 rounded">
        no history
      </div>
    )
  const mn = Math.min(...data)
  const mx = Math.max(...data)
  const rg = mx - mn || 1
  const pts = data
    .map(
      (v, i) =>
        `${(i / (data.length - 1)) * 124 + 2},${36 - ((v - mn) / rg) * 30}`,
    )
    .join(' ')
  const up = data[data.length - 1] >= data[0]
  const col = up ? '#34d399' : '#fb7185'
  const [lx, ly] = pts.split(' ').pop()!.split(',')
  return (
    <svg width="128" height="40" className="flex-none" aria-hidden="true">
      <polyline points={pts} fill="none" stroke={col} strokeWidth="1.5" />
      <polygon points={`2,38 ${pts} 126,38`} fill={col} opacity="0.12" />
      <circle cx={lx} cy={ly} r="2.5" fill={col} />
    </svg>
  )
}

/* ── Deployed strategy card (from monitor page) ─────────────── */

function StrategyCard({ c }: { c: Card }) {
  const flagged = c.flags.includes('thin_edge')
  return (
    <div
      className={`bg-surface border border-border rounded-xl p-4 hover:border-gray-600 transition-colors ${
        flagged ? 'border-l-4 border-l-amber-500' : ''
      } ${c.status === 'unreachable' ? 'opacity-70' : ''}`}
    >
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-[15px]">{c.id}</span>
        <span className="text-xs text-gray-400 truncate flex-1" title={c.name}>
          {c.name}
        </span>
        <span
          className={`text-[10px] px-2 py-0.5 rounded font-semibold uppercase ${
            MODE_STYLE[c.mode] ?? 'bg-gray-700/40 text-gray-300'
          }`}
        >
          {c.mode}
        </span>
        <span
          className={`text-[10px] px-2 py-0.5 rounded font-semibold ${
            STATUS_STYLE[c.status] ?? 'bg-gray-700/40 text-gray-300'
          }`}
        >
          {STATUS_LABEL[c.status] ?? c.status}
        </span>
      </div>

      {c.status === 'unreachable' ? (
        <div className="mt-4 text-sm text-gray-500">
          Data source unreachable (<span className="font-mono">{c.source}</span>)
          — stale numbers are hidden. Updates resume automatically on reconnect.
        </div>
      ) : (
        <>
          <div className="flex items-end justify-between mt-3 gap-3">
            <div>
              <div
                className={`text-2xl font-bold font-mono ${pnlColor(c.today_pct)}`}
              >
                {c.today_pct == null
                  ? '—'
                  : `${c.today_pct >= 0 ? '+' : ''}${c.today_pct.toFixed(2)}%`}
              </div>
              <div className="text-xs text-gray-500 mt-1 font-mono">
                today realized {fmt$(c.today_realized)} · unrealized {fmt$(c.unrealized)}
              </div>
            </div>
            <Spark data={c.spark} />
          </div>

          <div className="grid grid-cols-3 gap-x-3 gap-y-2 mt-3 pt-3 border-t border-border text-[13px]">
            <div>
              <div className="text-[10px] text-gray-600">Total return</div>
              <div className={`font-mono font-semibold ${pnlColor(c.total_return_pct)}`}>
                {c.total_return_pct == null
                  ? '—'
                  : `${c.total_return_pct >= 0 ? '+' : ''}${c.total_return_pct.toFixed(2)}%`}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">Equity</div>
              <div className="font-mono font-semibold">
                {c.equity == null ? '—' : `$${c.equity.toLocaleString('en-US', { maximumFractionDigits: 0 })}`}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">Positions</div>
              <div className="font-mono font-semibold">{c.positions ?? '—'}</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">Win rate × PF</div>
              <div className="font-mono font-semibold">
                {c.win_rate != null ? `${c.win_rate}%` : '—'}
                {c.profit_factor != null ? ` × ${c.profit_factor}` : ''}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">Expectancy</div>
              <div
                className={`font-mono font-semibold ${
                  c.expectancy == null
                    ? 'text-gray-500'
                    : c.expectancy < 0.03
                      ? 'text-amber-400'
                      : 'text-emerald-400'
                }`}
              >
                {c.expectancy == null ? 'low sample' : c.expectancy.toFixed(3)}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">Trades</div>
              <div className="font-mono font-semibold">{c.trades ?? '—'}</div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

/* ── Section heading ────────────────────────────────────────── */

function SectionHeading({ title, caption, href, hrefLabel }: {
  title: string
  caption?: string
  href?: string
  hrefLabel?: string
}) {
  return (
    <div className="flex items-baseline gap-3 flex-wrap">
      <h3 className="text-xs uppercase tracking-wider text-gray-400 font-semibold">
        {title}
      </h3>
      {caption && <span className="text-[11px] text-gray-600">{caption}</span>}
      {href && (
        <a href={href} className="text-[11px] text-accent hover:underline ml-auto">
          {hrefLabel ?? 'View all →'}
        </a>
      )}
    </div>
  )
}

function SectionError({ what, err }: { what: string; err: string }) {
  return (
    <div className="bg-surface border border-border rounded-xl p-4 text-sm text-gray-500">
      {what} unavailable — <span className="font-mono text-rose-400/80">{err}</span>
    </div>
  )
}

function SectionLoading({ what }: { what: string }) {
  return (
    <div className="bg-surface border border-border rounded-xl p-4 text-sm text-gray-500 animate-pulse">
      Loading {what}…
    </div>
  )
}

/* ── 2. Pipeline band ───────────────────────────────────────── */

const PIPELINE_STAGES: { key: string; label: string; style: string }[] = [
  { key: 'research', label: 'RESEARCH', style: 'border-purple-800/50 bg-purple-900/20 text-purple-300' },
  { key: 'validated', label: 'BACKTEST / VALIDATED', style: 'border-cyan-800/50 bg-cyan-900/20 text-cyan-300' },
  { key: 'paper', label: 'PAPER', style: 'border-sky-800/50 bg-sky-900/20 text-sky-300' },
  { key: 'live', label: 'LIVE', style: 'border-emerald-800/50 bg-emerald-900/20 text-emerald-400' },
]

function PipelineBand({ reg }: { reg: Registry }) {
  return (
    <div className="bg-surface border border-border rounded-xl p-4">
      <div className="flex items-center gap-2 flex-wrap">
        {PIPELINE_STAGES.map((st, i) => (
          <span key={st.key} className="flex items-center gap-2">
            {i > 0 && <span className="text-gray-600">→</span>}
            <a
              href="/strategies"
              className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-semibold tracking-wide hover:brightness-125 transition ${st.style}`}
            >
              {st.label}
              <span className="font-mono text-sm">{reg.counts[st.key] ?? 0}</span>
            </a>
          </span>
        ))}
        <span className="flex items-center gap-2 ml-auto">
          <a
            href="/strategies"
            className="px-2 py-1 rounded border border-border text-[10px] text-gray-500 hover:text-gray-300 transition-colors"
          >
            RETIRED <span className="font-mono">{reg.counts.retired ?? 0}</span>
          </a>
          <a
            href="/strategies"
            className="px-2 py-1 rounded border border-border text-[10px] text-gray-500 hover:text-gray-300 transition-colors"
          >
            REJECTED <span className="font-mono">{reg.counts.rejected ?? 0}</span>
          </a>
        </span>
      </div>
      <p className="text-[11px] text-gray-600 mt-2">
        Gate: OOS Sharpe ≥ 1.5 or auto-reject
      </p>
    </div>
  )
}

/* ── 4. Validated records strip ─────────────────────────────── */

function RecordsStrip({ payload }: { payload: RecordsPayload }) {
  const validated = payload.records.filter((r) => r.status === 'validated')
  const rejected = payload.records.filter((r) => r.status !== 'validated')

  return (
    <div className="space-y-3">
      {validated.length === 0 ? (
        <div className="bg-surface border border-border rounded-xl p-4 text-sm text-gray-500">
          No validated records yet.
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {validated.map((r) => (
            <a
              key={r.id}
              href="/backtest"
              className="bg-surface border border-emerald-800/40 rounded-xl p-4 hover:border-emerald-600/60 transition-colors block"
            >
              <div className="flex items-center gap-2">
                <span className="font-mono font-bold text-[15px]">{r.id}</span>
                <span className="text-[10px] px-2 py-0.5 rounded bg-emerald-500/15 text-emerald-400 font-semibold">
                  VALIDATED
                </span>
                {r.rerun && (
                  <span className="text-[10px] px-2 py-0.5 rounded bg-amber-500/15 text-amber-400 font-semibold uppercase">
                    rerun: {r.rerun}
                  </span>
                )}
              </div>
              <div className="flex gap-6 mt-3">
                <div>
                  <div className="text-[10px] text-gray-600 uppercase tracking-wider">Sharpe</div>
                  <div className="font-mono font-bold text-lg text-gray-100">
                    {r.headline.sharpe != null ? r.headline.sharpe.toFixed(2) : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-gray-600 uppercase tracking-wider">OOS Sharpe</div>
                  <div className="font-mono font-bold text-lg text-emerald-400">
                    {r.headline.oos_sharpe != null ? r.headline.oos_sharpe.toFixed(2) : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-gray-600 uppercase tracking-wider">Max DD</div>
                  <div className="font-mono font-bold text-lg text-rose-400">
                    {r.headline.max_dd != null ? `${(r.headline.max_dd * 100).toFixed(1)}%` : '—'}
                  </div>
                </div>
                {r.headline.cagr != null && (
                  <div>
                    <div className="text-[10px] text-gray-600 uppercase tracking-wider">CAGR</div>
                    <div className="font-mono font-bold text-lg text-gray-100">
                      {(r.headline.cagr * 100).toFixed(1)}%
                    </div>
                  </div>
                )}
              </div>
              <p className="text-xs text-gray-400 mt-3 leading-relaxed">{r.verdict}</p>
            </a>
          ))}
        </div>
      )}
      {rejected.length > 0 && (
        <a
          href="/backtest"
          className="block text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
        >
          {rejected.length} rejected by gate ({payload.gate_rule}) →
        </a>
      )}
    </div>
  )
}

/* ── 5. Ops strip ───────────────────────────────────────────── */

function OpsStrip({ ops }: { ops: OpsSummary }) {
  return (
    <div className="bg-surface border border-border rounded-xl p-4 flex items-center justify-between gap-4 flex-wrap">
      <div className="flex items-center gap-6 flex-wrap">
        <div>
          <div className="text-[10px] text-gray-500 uppercase tracking-wider">Deployments</div>
          <div className="font-mono font-bold text-lg">
            <span className={ops.running > 0 ? 'text-emerald-400' : 'text-gray-100'}>
              {ops.running}
            </span>
            <span className="text-gray-500 text-sm"> running</span>
            <span className="text-gray-600"> / </span>
            <span className={ops.online > 0 ? 'text-emerald-400' : 'text-gray-400'}>
              {ops.online}
            </span>
            <span className="text-gray-500 text-sm"> online</span>
            <span className="text-gray-600"> / </span>
            {ops.total_deployments}
            <span className="text-gray-500 text-sm"> total</span>
          </div>
        </div>
        <div>
          <div className="text-[10px] text-gray-500 uppercase tracking-wider">Session P&L</div>
          <div className={`font-mono font-bold text-lg ${pnlColor(ops.total_session_pnl)}`}>
            {fmt$(ops.total_session_pnl)}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-gray-500 uppercase tracking-wider">Session trades</div>
          <div className="font-mono font-bold text-lg">{ops.total_session_trades}</div>
        </div>
      </div>
      <a
        href="/cloud"
        className="px-3 py-1.5 bg-accent/20 hover:bg-accent/30 text-accent text-xs rounded-lg transition-colors font-medium"
      >
        Ops · Live →
      </a>
    </div>
  )
}

/* ── Main ───────────────────────────────────────────────────── */

export default function OverviewPage() {
  const monitor = usePolled<Snapshot>('/api/monitor/strategies', REFRESH_MS)
  const registry = usePolled<Registry>('/api/strategies/registry', 60_000)
  const records = usePolled<RecordsPayload>('/api/backtest/records', 60_000)
  const ops = usePolled<OpsSummary>('/api/deployments/summary', REFRESH_MS)

  const snap = monitor.data
  const deployed = snap?.strategies ?? []

  return (
    <div className="space-y-6">
      {/* ── 1. Header strip ──────────────────────────────── */}
      <div className="flex items-baseline gap-3 flex-wrap">
        <h2 className="text-2xl font-bold">Command Center</h2>
        <span className="text-xs text-gray-500">
          {monitor.err
            ? `backend unreachable (${monitor.err})`
            : snap
              ? `live · refreshes every ${REFRESH_MS / 1000}s · generated ${new Date(snap.generated_at).toLocaleTimeString()}`
              : 'loading…'}
        </span>
      </div>

      {monitor.err && !snap ? (
        <SectionError what="Portfolio rollup" err={monitor.err} />
      ) : snap ? (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {[
            ['Total equity', `$${snap.total_equity.toLocaleString('en-US', { maximumFractionDigits: 0 })}`, 'text-gray-100'],
            ['Today realized', fmt$(snap.today_realized), pnlColor(snap.today_realized)],
            ['Unrealized', fmt$(snap.unrealized), pnlColor(snap.unrealized)],
            ['Open positions', String(snap.open_positions), 'text-gray-100'],
          ].map(([l, v, cls]) => (
            <div key={l as string} className="bg-surface border border-border rounded-xl p-4">
              <div className="text-xs text-gray-500 uppercase tracking-wider">{l}</div>
              <div className={`text-xl font-bold font-mono mt-1 ${cls}`}>{v}</div>
            </div>
          ))}
        </div>
      ) : (
        <SectionLoading what="portfolio rollup" />
      )}

      {/* ── 2. Pipeline band ─────────────────────────────── */}
      <div className="space-y-2">
        <SectionHeading title="Strategy pipeline" href="/strategies" hrefLabel="Strategies →" />
        {registry.loading ? (
          <SectionLoading what="pipeline" />
        ) : registry.err || !registry.data ? (
          <SectionError what="Pipeline" err={registry.err ?? 'no data'} />
        ) : (
          <PipelineBand reg={registry.data} />
        )}
      </div>

      {/* ── 3. Deployed strategy cards ───────────────────── */}
      <div className="space-y-2">
        <SectionHeading
          title="Deployed strategies"
          caption="live + paper, real fills only"
          href="/monitor"
          hrefLabel="Monitor →"
        />
        {monitor.loading ? (
          <SectionLoading what="deployed strategies" />
        ) : monitor.err && !snap ? (
          <SectionError what="Deployed strategies" err={monitor.err} />
        ) : deployed.length === 0 ? (
          <div className="bg-surface border border-border rounded-xl p-4 text-sm text-gray-500">
            No deployed strategies reported.
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
            {deployed.map((c) => (
              <StrategyCard key={c.id} c={c} />
            ))}
          </div>
        )}
      </div>

      {/* ── 4. Validated records strip ───────────────────── */}
      <div className="space-y-2">
        <SectionHeading
          title="Validated research"
          caption={records.data ? `gate: ${records.data.gate_rule}` : undefined}
          href="/backtest"
          hrefLabel="Backtest →"
        />
        {records.loading ? (
          <SectionLoading what="research records" />
        ) : records.err || !records.data ? (
          <SectionError what="Research records" err={records.err ?? 'no data'} />
        ) : (
          <RecordsStrip payload={records.data} />
        )}
      </div>

      {/* ── 5. Ops strip ─────────────────────────────────── */}
      <div className="space-y-2">
        <SectionHeading title="Ops · Live runners" href="/cloud" hrefLabel="Ops →" />
        {ops.loading ? (
          <SectionLoading what="deployments" />
        ) : ops.err || !ops.data ? (
          <SectionError what="Deployments" err={ops.err ?? 'no data'} />
        ) : (
          <OpsStrip ops={ops.data} />
        )}
      </div>
    </div>
  )
}
