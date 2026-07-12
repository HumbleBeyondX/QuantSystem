'use client'

import Link from 'next/link'
import { useState, useEffect } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'

/**
 * Strategies Page — Lifecycle Registry
 *
 * Fetches the lifecycle registry from /api/strategies/registry and groups
 * strategies by stage: LIVE → PAPER → VALIDATED → RESEARCH → RETIRED → REJECTED.
 * Each card shows honest metrics, the OOS gate verdict, and links to the
 * official record (/backtest) or deployment (/cloud) when available.
 * Deployed strategies (from /api/strategies/) are merged in to show live P&L.
 */

interface GateInfo {
  threshold: number
  oos_sharpe?: number | null
  passed: boolean | null
  note?: string | null
}

interface PromotionLink {
  stage: string
  promoted_at: string
  forced: boolean
  reason?: string | null
  throttle: number
}

interface StrategyLinks {
  record?: string | null
  deployment?: string | null
  paper_log?: string | null
  promotion?: PromotionLink | null
}

interface PromotionCheck {
  id: string
  name: string
  passed: boolean | null
  detail: string
}

interface PromotionReport {
  strategy_id: string
  ready: boolean
  throttle: number
  evaluated_at: string
  checks: PromotionCheck[]
}

interface GoLiveResult {
  stage: string
  forced: boolean
  throttle: number
  next_steps: string[]
}

interface RegistryStrategy {
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
  stage: string
  gate: GateInfo
  metrics: Record<string, number | null>
  links: StrategyLinks
  notes: string
  runnable: boolean
}

interface RegistryResponse {
  stages: string[]
  groups: Record<string, RegistryStrategy[]>
  gate_rule: string
  counts: Record<string, number>
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

const DEFAULT_STAGES = ['live', 'paper', 'validated', 'research', 'retired', 'rejected']

const stageBadge: Record<string, string> = {
  live: 'bg-green-500/20 text-green-400',
  paper: 'bg-blue-500/20 text-blue-400',
  validated: 'bg-cyan-500/20 text-cyan-400',
  research: 'bg-gray-500/20 text-gray-400',
  retired: 'bg-zinc-500/20 text-zinc-500',
  rejected: 'bg-red-500/20 text-red-400',
}

const stageDot: Record<string, string> = {
  live: 'bg-green-400',
  paper: 'bg-blue-400',
  validated: 'bg-cyan-400',
  research: 'bg-gray-400',
  retired: 'bg-zinc-500',
  rejected: 'bg-red-400',
}

// Metric keys we know how to render, in display order
const METRIC_DEFS: Array<{ key: string; label: string; fmt: (v: number) => string }> = [
  { key: 'backtest_sharpe', label: 'Sharpe', fmt: v => v.toFixed(2) },
  { key: 'oos_sharpe', label: 'OOS Sharpe', fmt: v => v.toFixed(2) },
  { key: 'walkforward_sharpe', label: 'WF Sharpe', fmt: v => v.toFixed(2) },
  { key: 'max_dd', label: 'Max DD', fmt: v => `${(Math.abs(v) <= 1 ? v * 100 : v).toFixed(1)}%` },
  { key: 'cagr', label: 'CAGR', fmt: v => `${(Math.abs(v) <= 1 ? v * 100 : v).toFixed(1)}%` },
  { key: 'correlation', label: 'Corr', fmt: v => v.toFixed(2) },
  { key: 'win_rate', label: 'Win Rate', fmt: v => `${(v <= 1 ? v * 100 : v).toFixed(0)}%` },
  { key: 'trades', label: 'Trades', fmt: v => v.toLocaleString() },
]

function GateChip({ gate }: { gate: GateInfo }) {
  if (!gate) return null
  const oos = gate.oos_sharpe != null ? gate.oos_sharpe.toFixed(2) : '?'
  return (
    <div className="space-y-1">
      {gate.passed === true ? (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium bg-green-500/10 text-green-400 border border-green-500/30">
          GATE &#10003; OOS {oos} &ge; {gate.threshold}
        </span>
      ) : gate.passed === false ? (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium bg-red-500/10 text-red-400 border border-red-500/30">
          GATE &#10007; OOS {oos} &lt; {gate.threshold}
        </span>
      ) : (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-medium bg-gray-500/10 text-gray-400 border border-gray-500/30">
          GATE &mdash; pending
        </span>
      )}
      {gate.note && <p className="text-[10px] text-gray-500 italic">{gate.note}</p>}
    </div>
  )
}

function CheckIcon({ passed }: { passed: boolean | null }) {
  if (passed === true) return <span className="text-emerald-400 font-bold">&#10003;</span>
  if (passed === false) return <span className="text-rose-400 font-bold">&#10007;</span>
  return <span className="text-gray-500 font-bold">&#9675;</span>
}

function PromotionPanel({
  strategyId,
  onPromoted,
}: {
  strategyId: string
  onPromoted: () => void
}) {
  const [report, setReport] = useState<PromotionReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [result, setResult] = useState<GoLiveResult | null>(null)
  const [forceOpen, setForceOpen] = useState(false)
  const [forceReason, setForceReason] = useState('')

  const fetchReport = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch(`${API}/api/strategies/${strategyId}/promotion`)
      if (res.ok) setReport(await res.json())
      else setError(`Failed to load promotion report (${res.status})`)
    } catch (e: any) {
      setError(`Cannot reach backend: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchReport()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyId])

  const promote = async (force: boolean) => {
    setSubmitting(true)
    setActionError(null)
    try {
      const body: Record<string, unknown> = { confirm: true }
      if (force) {
        body.force = true
        body.reason = forceReason.trim()
      }
      const res = await fetch(`${API}/api/strategies/${strategyId}/go-live`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await res.json().catch(() => null)
      if (res.ok) {
        setResult(data as GoLiveResult)
        onPromoted()
      } else {
        setActionError(data?.detail ? String(data.detail) : `Promotion failed (${res.status})`)
      }
    } catch (e: any) {
      setActionError(`Cannot reach backend: ${e.message}`)
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    return (
      <div className="mt-3 p-3 rounded-lg bg-background border border-border/50 space-y-3">
        <div className="px-2 py-1.5 rounded bg-green-500/10 border border-green-500/30 text-green-400 text-xs font-bold">
          PROMOTED — throttle {(result.throttle * 100).toFixed(0)}%{result.forced ? ' · FORCED' : ''}
        </div>
        {result.next_steps?.length > 0 && (
          <ol className="list-decimal list-inside space-y-1 font-mono text-[11px] text-gray-400">
            {result.next_steps.map((step, i) => (
              <li key={i}>{step}</li>
            ))}
          </ol>
        )}
      </div>
    )
  }

  const passedCount = report ? report.checks.filter(c => c.passed === true).length : 0

  return (
    <div className="mt-3 p-3 rounded-lg bg-background border border-border/50 space-y-3">
      {loading && <p className="text-xs text-gray-500 animate-pulse">Evaluating promotion gates...</p>}
      {error && <p className="text-xs text-rose-400">{error}</p>}

      {!loading && report && (
        <>
          <div className="flex items-center justify-between gap-2">
            <p className={`text-xs font-bold ${report.ready ? 'text-green-400' : 'text-gray-300'}`}>
              {report.ready
                ? 'READY FOR LIVE'
                : `NOT READY — ${passedCount}/${report.checks.length} gates passed`}
            </p>
            <button
              onClick={fetchReport}
              className="text-[11px] text-gray-400 hover:text-gray-100 border border-border rounded px-2 py-0.5 transition-colors"
            >
              Re-check
            </button>
          </div>
          <p className="text-[10px] text-gray-500">
            Evaluated {new Date(report.evaluated_at).toLocaleString()}
          </p>

          <div className="space-y-1.5">
            {report.checks.map(check => (
              <div key={check.id} className="flex items-start gap-2 text-xs">
                <CheckIcon passed={check.passed} />
                <div className="min-w-0">
                  <span className="text-gray-200">{check.name}</span>
                  <p className="text-[10px] text-gray-500">{check.detail}</p>
                </div>
              </div>
            ))}
          </div>

          <button
            onClick={() => promote(false)}
            disabled={!report.ready || submitting}
            className={`w-full py-1.5 rounded-lg text-xs font-semibold transition-colors ${
              report.ready && !submitting
                ? 'bg-accent text-white hover:bg-accent/80'
                : 'bg-surface border border-border text-gray-600 cursor-not-allowed'
            }`}
          >
            {submitting ? 'Promoting...' : 'Promote to Live'}
          </button>

          {actionError && (
            <p className="text-[11px] text-rose-400 break-words">{actionError}</p>
          )}

          <div className="pt-1 border-t border-border/50">
            <button
              onClick={() => setForceOpen(o => !o)}
              className="text-[11px] text-gray-500 hover:text-gray-300 transition-colors"
            >
              {forceOpen ? '▾' : '▸'} Force promotion (override gates)
            </button>
            {forceOpen && (
              <div className="mt-2 space-y-2">
                <p className="text-[10px] text-rose-400">
                  Recorded permanently as FORCED in the audit trail
                </p>
                <input
                  type="text"
                  value={forceReason}
                  onChange={e => setForceReason(e.target.value)}
                  placeholder="Reason (required)"
                  className="w-full bg-surface border border-border rounded px-2 py-1 text-xs text-gray-100 placeholder-gray-600 focus:outline-none focus:border-rose-500/50"
                />
                <button
                  onClick={() => promote(true)}
                  disabled={forceReason.trim() === '' || submitting}
                  className={`w-full py-1.5 rounded-lg text-xs font-semibold transition-colors ${
                    forceReason.trim() !== '' && !submitting
                      ? 'bg-rose-500/20 text-rose-400 border border-rose-500/40 hover:bg-rose-500/30'
                      : 'bg-surface border border-border text-gray-600 cursor-not-allowed'
                  }`}
                >
                  {submitting ? 'Promoting...' : 'Force Promote'}
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function DemoteControl({
  strategyId,
  onDemoted,
}: {
  strategyId: string
  onDemoted: () => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const demote = async () => {
    setSubmitting(true)
    setError(null)
    try {
      const res = await fetch(`${API}/api/strategies/${strategyId}/demote`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reason: reason.trim() }),
      })
      const data = await res.json().catch(() => null)
      if (res.ok) {
        onDemoted()
      } else {
        setError(data?.detail ? String(data.detail) : `Demotion failed (${res.status})`)
      }
    } catch (e: any) {
      setError(`Cannot reach backend: ${e.message}`)
    } finally {
      setSubmitting(false)
    }
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="text-[10px] text-gray-500 hover:text-gray-300 underline decoration-dotted transition-colors"
      >
        Demote to paper
      </button>
    )
  }

  return (
    <div className="mt-2 space-y-2 w-full">
      <input
        type="text"
        value={reason}
        onChange={e => setReason(e.target.value)}
        placeholder="Demotion reason (required)"
        className="w-full bg-surface border border-border rounded px-2 py-1 text-xs text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent/50"
      />
      <div className="flex items-center gap-2">
        <button
          onClick={demote}
          disabled={reason.trim() === '' || submitting}
          className={`text-[11px] px-2 py-1 rounded transition-colors ${
            reason.trim() !== '' && !submitting
              ? 'bg-warning/10 text-warning border border-warning/30 hover:bg-warning/20'
              : 'bg-surface border border-border text-gray-600 cursor-not-allowed'
          }`}
        >
          {submitting ? 'Demoting...' : 'Confirm demote'}
        </button>
        <button
          onClick={() => setOpen(false)}
          className="text-[11px] text-gray-500 hover:text-gray-300"
        >
          Cancel
        </button>
      </div>
      {error && <p className="text-[11px] text-rose-400 break-words">{error}</p>}
    </div>
  )
}

function StrategyCard({
  strategy,
  deployed,
  monitorStatus,
  onRefresh,
}: {
  strategy: RegistryStrategy
  deployed: DeployedStrategy | undefined
  monitorStatus: string | undefined
  onRefresh: () => void
}) {
  const [promoOpen, setPromoOpen] = useState(false)
  const isRunning = deployed?.status === 'running'
  const pnl = deployed ? deployed.realized_pnl + deployed.unrealized_pnl : null

  const metricEntries = METRIC_DEFS.filter(
    d => strategy.metrics && strategy.metrics[d.key] != null && typeof strategy.metrics[d.key] === 'number'
  )

  const dimmed = strategy.stage === 'retired' || strategy.stage === 'rejected'

  return (
    <div
      className={`bg-surface rounded-xl border border-border p-5 hover:border-accent/50 transition-all group flex flex-col ${
        dimmed ? 'opacity-70' : ''
      }`}
    >
      <div className="flex items-start justify-between mb-2">
        <div className="min-w-0">
          <Link
            href={`/strategies/${strategy.id}`}
            className="font-semibold text-gray-100 group-hover:text-accent transition-colors"
          >
            {strategy.name}
          </Link>
          <div className="flex items-center gap-2 mt-1 flex-wrap">
            <span
              className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${
                stageBadge[strategy.stage] ?? 'bg-gray-500/20 text-gray-400'
              }`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${stageDot[strategy.stage] ?? 'bg-gray-400'}`} />
              {strategy.stage}
            </span>
            <span className="text-[10px] text-gray-500">{assetClassLabels[strategy.asset_class] ?? strategy.asset_class}</span>
            {strategy.symbols.length > 0 && (
              <span className="text-[10px] text-gray-500">
                {strategy.symbols.slice(0, 3).join(', ')}
                {strategy.symbols.length > 3 && ` +${strategy.symbols.length - 3}`}
              </span>
            )}
          </div>
        </div>
        {(deployed || monitorStatus) && (() => {
          // deployment record wins; otherwise live telemetry from /api/monitor
          const status = deployed ? deployed.status : (monitorStatus as string)
          const good = status === 'running' || status === 'flat'
          return (
            <span
              className={`flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-full flex-shrink-0 ${
                good ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning'
              }`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${good ? 'bg-success animate-pulse' : 'bg-warning'}`} />
              {status.charAt(0).toUpperCase() + status.slice(1)}
            </span>
          )
        })()}
      </div>

      <p className="text-xs text-gray-500 mb-3 line-clamp-2">{strategy.description}</p>

      {/* Gate verdict */}
      <div className="mb-3">
        <GateChip gate={strategy.gate} />
      </div>

      {/* Promotion provenance (live via promotion system) */}
      {strategy.stage === 'live' && strategy.links?.promotion && (
        <div className="mb-3 flex flex-col items-start gap-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-[10px] font-medium bg-green-500/10 text-green-400 border border-green-500/30">
              PROMOTED {new Date(strategy.links.promotion.promoted_at).toLocaleDateString()}
              {strategy.links.promotion.forced && <span className="text-rose-400"> &middot; FORCED</span>}
            </span>
            <span className="text-[10px] text-gray-500">
              throttle {(strategy.links.promotion.throttle * 100).toFixed(0)}%
            </span>
          </div>
          <DemoteControl strategyId={strategy.id} onDemoted={onRefresh} />
        </div>
      )}

      {/* Live P&L for deployed strategies */}
      {deployed && pnl !== null && (
        <div className="grid grid-cols-3 gap-3 mb-3 p-2.5 rounded-lg bg-background border border-border/50">
          <div>
            <p className="text-[10px] text-gray-500">Live P&amp;L</p>
            <p className={`text-sm font-semibold ${pnl >= 0 ? 'text-success' : 'text-danger'}`}>
              {pnl >= 0 ? '+' : ''}${pnl.toLocaleString(undefined, { minimumFractionDigits: 2 })}
            </p>
          </div>
          <div>
            <p className="text-[10px] text-gray-500">Win Rate</p>
            <p className="text-sm font-semibold text-gray-100">
              {deployed.win_rate != null ? `${deployed.win_rate}%` : '—'}
            </p>
          </div>
          <div>
            <p className="text-[10px] text-gray-500">Trades</p>
            <p className="text-sm font-semibold text-gray-100">{deployed.total_trades}</p>
          </div>
        </div>
      )}

      {/* Honest metrics */}
      {metricEntries.length > 0 && (
        <div className="grid grid-cols-3 gap-x-3 gap-y-2 pt-3 border-t border-border">
          {metricEntries.map(d => (
            <div key={d.key}>
              <p className="text-[10px] text-gray-500">{d.label}</p>
              <p className="text-sm font-semibold text-gray-100">{d.fmt(strategy.metrics[d.key] as number)}</p>
            </div>
          ))}
        </div>
      )}

      {/* Notes */}
      {strategy.notes && (
        <p className="text-[11px] text-gray-500 mt-3 leading-relaxed">{strategy.notes}</p>
      )}

      {/* Links */}
      {(strategy.links?.record || strategy.links?.deployment) && (
        <div className="flex items-center gap-3 mt-auto pt-3">
          {strategy.links?.record && (
            <Link href="/backtest" className="text-[11px] text-accent hover:underline">
              Official record &rarr;
            </Link>
          )}
          {strategy.links?.deployment && (
            <Link href="/cloud" className="text-[11px] text-accent hover:underline">
              Deployment &rarr;
            </Link>
          )}
        </div>
      )}

      {/* Paper → Live promotion */}
      {strategy.stage === 'paper' && (
        <div className="mt-auto pt-3">
          <button
            onClick={() => setPromoOpen(o => !o)}
            className="px-3 py-1 rounded-lg text-xs font-semibold bg-accent text-white hover:bg-accent/80 transition-colors"
          >
            {promoOpen ? 'Hide promotion panel' : 'Go Live'}
          </button>
          {promoOpen && <PromotionPanel strategyId={strategy.id} onPromoted={onRefresh} />}
        </div>
      )}
    </div>
  )
}

export default function StrategiesPage() {
  const [filter, setFilter] = useState<string>('All')
  const [registry, setRegistry] = useState<RegistryResponse | null>(null)
  const [deployed, setDeployed] = useState<DeployedStrategy[]>([])
  const [monitorStatus, setMonitorStatus] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const fetchData = async () => {
    try {
      const [regRes, depRes, monRes] = await Promise.all([
        fetch(`${API}/api/strategies/registry`),
        fetch(`${API}/api/strategies/`),
        fetch(`${API}/api/monitor/strategies`),
      ])
      if (regRes.ok) setRegistry(await regRes.json())
      else setError('Failed to load strategy registry')
      if (depRes.ok) setDeployed(await depRes.json())
      if (monRes.ok) {
        const snap = await monRes.json()
        const map: Record<string, string> = {}
        for (const card of snap.strategies ?? []) {
          // monitor ids -> registry ids
          const rid = card.id === 'gamma_scalp' ? 'spy_gamma_scalp' : card.id
          if (card.status) map[rid] = card.status
        }
        setMonitorStatus(map)
      }
    } catch (e: any) {
      setError(`Cannot connect to backend: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    fetchData()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const deployedMap = new Map(deployed.map(d => [d.strategy_id, d]))
  const stages = registry?.stages?.length ? registry.stages : DEFAULT_STAGES
  const totalCount = registry
    ? Object.values(registry.groups).reduce((n, g) => n + g.length, 0)
    : 0

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Strategies</h2>
          {registry?.gate_rule && (
            <p className="text-xs text-gray-500 mt-1">
              Lifecycle gate: <span className="text-gray-400">{registry.gate_rule}</span>
            </p>
          )}
        </div>
        <span className="text-sm text-gray-400">
          {loading ? '...' : `${totalCount} strategies`}
        </span>
      </div>

      {/* Asset-class Filter Tabs */}
      <div className="flex gap-2">
        {assetClasses.map(ac => (
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

      {/* Lifecycle Sections */}
      {!loading &&
        registry &&
        stages.map(stage => {
          const group = registry.groups[stage] ?? []
          const filtered = filter === 'All' ? group : group.filter(s => s.asset_class === filter)
          if (filtered.length === 0) return null
          return (
            <section key={stage} className="space-y-3">
              <div className="flex items-center gap-3">
                <span className={`w-2 h-2 rounded-full ${stageDot[stage] ?? 'bg-gray-400'}`} />
                <h3 className="text-sm font-bold uppercase tracking-widest text-gray-300">{stage}</h3>
                <span className="text-xs text-gray-500">
                  {filtered.length}
                  {filter !== 'All' && registry.counts?.[stage] != null && filtered.length !== registry.counts[stage]
                    ? ` of ${registry.counts[stage]}`
                    : ''}
                </span>
                <div className="flex-1 h-px bg-border" />
              </div>
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                {filtered.map(strategy => (
                  <StrategyCard
                    key={strategy.id}
                    strategy={strategy}
                    deployed={deployedMap.get(strategy.id)}
                    monitorStatus={monitorStatus[strategy.id]}
                    onRefresh={fetchData}
                  />
                ))}
              </div>
            </section>
          )
        })}
    </div>
  )
}
