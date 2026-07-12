'use client'

import { useCallback, useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8002'

// ── Types ────────────────────────────────────────────────────

interface DeploymentInfo {
  id: string
  name: string
  strategy_id: string
  host: string
  port: number
  region: string
  region_label: string
  exchange: string
  symbol: string
  asset_class: string
  enabled: boolean
  tags: string[]
}

interface RunnerSnapshot {
  running: boolean
  emergency_stopped: boolean
  strategy_id: string
  exchange: string
  sandbox: boolean
  symbol: string
  session_pnl: number
  session_trades: number
  session_orders: number
  session_start: string | null
  position_manager_snapshot: {
    total_value: number
    cash: number
    unrealized_pnl: number
    realized_pnl: number
    num_positions: number
    positions: { symbol: string; quantity: number; avg_price: number; unrealized_pnl: number }[]
  } | null
}

interface DeploymentStatus {
  deployment: DeploymentInfo
  online: boolean
  runner_status: RunnerSnapshot | null
  latency_ms: number | null
  error: string | null
  last_checked: string | null
}

interface Summary {
  total_deployments: number
  online: number
  running: number
  total_session_pnl: number
  total_session_trades: number
  total_session_orders: number
}

// Minimal slice of the strategy registry needed to detect retired strategies
interface RegistryStrategyLite {
  id: string
  name: string
  stage: string
  notes?: string | null
  gate?: { note?: string | null } | null
  metrics?: Record<string, number | null> | null
  links?: {
    deployment?: string | null
    promotion?: { reason?: string | null } | null
  } | null
}

interface RetirementInfo {
  stage: string
  reason: string | null
  finalPnl: number | null
}

// ── Helpers ──────────────────────────────────────────────────

const regionFlags: Record<string, string> = {
  'ap-northeast-1': 'JP',
  'ap-southeast-1': 'SG',
  'ap-east-1': 'HK',
  'us-east-1': 'US',
  'us-west-2': 'US',
  'eu-west-1': 'IE',
  'eu-central-1': 'DE',
}

function formatPnl(v: number): string {
  const sign = v >= 0 ? '+' : ''
  return `${sign}$${v.toFixed(2)}`
}

function formatTime(iso: string | null): string {
  if (!iso) return '--'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

function timeSince(iso: string | null): string {
  if (!iso) return '--'
  const diff = Date.now() - new Date(iso).getTime()
  const hrs = Math.floor(diff / 3600000)
  const mins = Math.floor((diff % 3600000) / 60000)
  if (hrs > 24) return `${Math.floor(hrs / 24)}d ${hrs % 24}h`
  if (hrs > 0) return `${hrs}h ${mins}m`
  return `${mins}m`
}

function excerpt(text?: string | null, max = 180): string | null {
  if (!text) return null
  const clean = text.trim().replace(/\s+/g, ' ')
  if (!clean) return null
  return clean.length > max ? `${clean.slice(0, max - 1)}…` : clean
}

function retirementInfo(s: RegistryStrategyLite): RetirementInfo {
  const pnl = s.metrics?.final_realized_pnl_usd
  return {
    stage: s.stage,
    reason: s.links?.promotion?.reason || excerpt(s.notes) || excerpt(s.gate?.note),
    finalPnl: typeof pnl === 'number' ? pnl : null,
  }
}

// ── Main Component ──────────────────────────────────────────

interface ReportStatus {
  running: boolean
  enabled: boolean
  send_time_utc: string
  recipient: string
  last_sent_date: string | null
  last_run_at: string | null
  last_error: string | null
}

export default function CloudPage() {
  const [deployments, setDeployments] = useState<DeploymentStatus[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)
  const [retiredStrategies, setRetiredStrategies] = useState<RegistryStrategyLite[]>([])
  const [loading, setLoading] = useState(true)
  const [actionLoading, setActionLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expandedLogs, setExpandedLogs] = useState<string | null>(null)
  const [logs, setLogs] = useState<string[]>([])
  const [logsLoading, setLogsLoading] = useState(false)
  const [reportStatus, setReportStatus] = useState<ReportStatus | null>(null)
  const [reportSending, setReportSending] = useState(false)
  const [reportToast, setReportToast] = useState<string | null>(null)
  const [localRunner, setLocalRunner] = useState<RunnerSnapshot | null>(null)
  const [runnerLoading, setRunnerLoading] = useState<string | null>(null)

  const fetchAll = useCallback(async () => {
    try {
      const [depRes, sumRes, repRes, regRes] = await Promise.all([
        fetch(`${API}/api/deployments`),
        fetch(`${API}/api/deployments/summary`),
        fetch(`${API}/api/reports/daily/status`),
        fetch(`${API}/api/strategies/registry`),
      ])
      if (depRes.ok) setDeployments(await depRes.json())
      if (sumRes.ok) setSummary(await sumRes.json())
      if (repRes.ok) setReportStatus(await repRes.json())
      if (regRes.ok) {
        const reg = await regRes.json()
        const groups = reg?.groups ?? {}
        setRetiredStrategies([...(groups.retired ?? []), ...(groups.rejected ?? [])])
      }
      setError(null)
    } catch (e: any) {
      setError('Backend not reachable. Start local dashboard first.')
    } finally {
      setLoading(false)
    }
  }, [])

  const sendReportNow = async () => {
    setReportSending(true)
    setReportToast(null)
    try {
      const res = await fetch(`${API}/api/reports/daily/send-now`, { method: 'POST' })
      if (res.ok) {
        setReportToast('Report emailed successfully')
      } else {
        const body = await res.json().catch(() => ({ detail: 'unknown' }))
        setReportToast(`Send failed: ${body.detail}`)
      }
      await fetchAll()
    } catch (e: any) {
      setReportToast(`Error: ${e.message}`)
    } finally {
      setReportSending(false)
      setTimeout(() => setReportToast(null), 5000)
    }
  }

  const openReportPreview = () => {
    window.open(`${API}/api/reports/daily/preview.html`, '_blank')
  }

  // Fetch local standalone runner status (renders only when a runner is active)
  const fetchLocalRunner = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/live/runner/status`)
      if (!res.ok) {
        setLocalRunner(null)
        return
      }
      const data = await res.json()
      // Backend returns {running: false, message: ...} when no runner exists —
      // only treat payloads with a strategy_id as a real runner.
      setLocalRunner(data && data.strategy_id ? data : null)
    } catch {
      setLocalRunner(null)
    }
  }, [])

  const doRunnerAction = async (action: 'emergency-stop' | 'force-close') => {
    setRunnerLoading(action)
    setError(null)
    try {
      const res = await fetch(`${API}/api/live/runner/${action}`, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }))
        setError(body.detail || body.message || 'Action failed')
      }
      await fetchLocalRunner()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setRunnerLoading(null)
    }
  }

  // Poll every 5 seconds
  useEffect(() => {
    fetchAll()
    const interval = setInterval(fetchAll, 5000)
    return () => clearInterval(interval)
  }, [fetchAll])

  // Poll local runner every 10 seconds
  useEffect(() => {
    fetchLocalRunner()
    const interval = setInterval(fetchLocalRunner, 10000)
    return () => clearInterval(interval)
  }, [fetchLocalRunner])

  // Actions
  const doAction = async (deployId: string, action: string) => {
    setActionLoading(`${deployId}:${action}`)
    setError(null)
    try {
      const res = await fetch(`${API}/api/deployments/${deployId}/${action}`, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }))
        setError(body.detail || 'Action failed')
      }
      await fetchAll()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setActionLoading(null)
    }
  }

  const fetchLogs = async (deployId: string) => {
    if (expandedLogs === deployId) {
      setExpandedLogs(null)
      return
    }
    setLogsLoading(true)
    setExpandedLogs(deployId)
    try {
      const res = await fetch(`${API}/api/deployments/${deployId}/logs?lines=30`)
      if (res.ok) {
        const data = await res.json()
        setLogs(data.lines || [])
      }
    } catch {
      setLogs(['Failed to fetch logs'])
    } finally {
      setLogsLoading(false)
    }
  }

  // ── Zone classification ────────────────────────────────────
  // Registry lifecycle stage wins over live probe status: a deployment whose
  // strategy is retired/rejected is HISTORICAL even if its VPS runner still
  // reports emergency-stopped or online.
  const retiredById = new Map(retiredStrategies.map(s => [s.id, s]))

  const historicalDeployments = deployments.filter(d => retiredById.has(d.deployment.strategy_id))
  const activeDeployments = deployments.filter(d => !retiredById.has(d.deployment.strategy_id))
  const runningDeployments = activeDeployments.filter(
    d => d.online && d.runner_status?.running && !d.runner_status?.emergency_stopped
  )
  const stoppedDeployments = activeDeployments.filter(d => !runningDeployments.includes(d))

  // Retired registry strategies that reference a deployment whose record is
  // gone — still shown as historical ghost cards.
  const ghostRetired = retiredStrategies.filter(
    s =>
      s.links?.deployment &&
      !deployments.some(
        d => d.deployment.strategy_id === s.id || d.deployment.id === s.links?.deployment
      )
  )

  const renderCard = (d: DeploymentStatus, historical: RetirementInfo | null = null) => (
    <DeploymentCard
      key={d.deployment.id}
      data={d}
      historical={historical}
      actionLoading={actionLoading}
      onEmergencyStop={() => doAction(d.deployment.id, 'emergency-stop')}
      onForceClose={() => doAction(d.deployment.id, 'force-close')}
      onRestart={() => doAction(d.deployment.id, 'restart')}
      onToggleLogs={() => fetchLogs(d.deployment.id)}
      logsExpanded={expandedLogs === d.deployment.id}
      logs={expandedLogs === d.deployment.id ? logs : []}
      logsLoading={logsLoading && expandedLogs === d.deployment.id}
    />
  )

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold">Ops &middot; Live Execution</h1>
        <p className="text-sm text-gray-500 mt-1">
          Manage live trading strategies across cloud servers
        </p>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Summary Cards */}
      {summary && (
        <div className="grid grid-cols-5 gap-4">
          <SummaryCard label="Deployments" value={summary.total_deployments} />
          <SummaryCard label="Online" value={summary.online} color={summary.online > 0 ? 'green' : 'gray'} />
          <SummaryCard label="Running" value={summary.running} color={summary.running > 0 ? 'green' : 'yellow'} />
          <SummaryCard
            label="Session P&L"
            value={formatPnl(summary.total_session_pnl)}
            color={summary.total_session_pnl >= 0 ? 'green' : 'red'}
          />
          <SummaryCard label="Trades" value={summary.total_session_trades} />
        </div>
      )}

      {/* Deployment Zones */}
      {loading ? (
        <div className="bg-surface rounded-xl border border-border p-12 text-center">
          <p className="text-gray-500 animate-pulse">Loading deployments...</p>
        </div>
      ) : deployments.length === 0 && ghostRetired.length === 0 ? (
        <div className="bg-surface rounded-xl border border-border p-12 text-center">
          <p className="text-gray-500 text-lg">No deployments configured</p>
          <p className="text-gray-600 text-sm mt-2">
            Add entries to <code className="bg-background px-1.5 py-0.5 rounded text-xs">config/deployments.yaml</code> and restart the dashboard
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {runningDeployments.length > 0 && (
            <section className="space-y-3">
              <ZoneHeader title="RUNNING" count={runningDeployments.length} dot="bg-green-500" />
              <div className="space-y-4">{runningDeployments.map(d => renderCard(d))}</div>
            </section>
          )}

          {stoppedDeployments.length > 0 && (
            <section className="space-y-3">
              <ZoneHeader title="STOPPED" count={stoppedDeployments.length} dot="bg-amber-500" />
              <div className="space-y-4">{stoppedDeployments.map(d => renderCard(d))}</div>
            </section>
          )}

          {(historicalDeployments.length > 0 || ghostRetired.length > 0) && (
            <section className="space-y-3">
              <ZoneHeader
                title="HISTORICAL"
                hint="retired"
                count={historicalDeployments.length + ghostRetired.length}
                dot="bg-zinc-500"
              />
              <div className="space-y-4">
                {historicalDeployments.map(d =>
                  renderCard(d, retirementInfo(retiredById.get(d.deployment.strategy_id)!))
                )}
                {ghostRetired.map(s => (
                  <GhostHistoricalCard key={s.id} strategy={s} info={retirementInfo(s)} />
                ))}
              </div>
            </section>
          )}
        </div>
      )}

      {/* Local Standalone Runner (renders only when a runner is active) */}
      {localRunner && (
        <div className="bg-surface rounded-xl border border-green-800/50 p-5">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-3">
              <span className={`w-3 h-3 rounded-full ${localRunner.emergency_stopped ? 'bg-red-500' : 'bg-green-500 animate-pulse'}`} />
              <div>
                <h2 className="text-lg font-semibold">
                  Local Runner &middot; {localRunner.strategy_id}
                </h2>
                <p className="text-xs text-gray-500">
                  {localRunner.exchange?.toUpperCase()} / {localRunner.symbol}
                  {localRunner.sandbox ? ' (TESTNET)' : ' (MAINNET)'}
                </p>
              </div>
            </div>
            <span className={`text-sm font-semibold ${localRunner.emergency_stopped ? 'text-red-400' : 'text-green-400'}`}>
              {localRunner.emergency_stopped ? 'EMERGENCY STOPPED' : 'RUNNING'}
            </span>
          </div>

          <div className="grid grid-cols-4 gap-3 mb-4">
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Session P&L</p>
              <p className={`text-lg font-mono font-bold ${localRunner.session_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {formatPnl(localRunner.session_pnl)}
              </p>
            </div>
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Trades</p>
              <p className="text-lg font-mono font-bold">{localRunner.session_trades}</p>
            </div>
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Orders</p>
              <p className="text-lg font-mono font-bold">{localRunner.session_orders}</p>
            </div>
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Started</p>
              <p className="text-sm font-mono text-gray-300">
                {formatTime(localRunner.session_start)}
              </p>
            </div>
          </div>

          {localRunner.position_manager_snapshot && localRunner.position_manager_snapshot.positions.length > 0 && (
            <div className="mb-4 bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">Open Positions</p>
              {localRunner.position_manager_snapshot.positions.map((pos, i) => (
                <div key={i} className="flex items-center justify-between text-sm py-1">
                  <span className="font-mono text-gray-300">{pos.symbol}</span>
                  <span className={pos.quantity > 0 ? 'text-green-400' : 'text-red-400'}>
                    {pos.quantity > 0 ? 'LONG' : 'SHORT'} {Math.abs(pos.quantity).toFixed(4)}
                  </span>
                  <span className="font-mono text-gray-400">@ ${pos.avg_price.toFixed(2)}</span>
                  <span className={`font-mono ${pos.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {formatPnl(pos.unrealized_pnl)}
                  </span>
                </div>
              ))}
            </div>
          )}

          <div className="flex gap-2">
            <ConfirmButton
              label="Emergency Stop"
              onConfirm={() => doRunnerAction('emergency-stop')}
              disabled={runnerLoading !== null || localRunner.emergency_stopped}
              sizeClassName="px-4 py-2 text-sm"
              idleClassName="bg-red-800 hover:bg-red-700 text-white"
            />
            <ConfirmButton
              label="Force Close Positions"
              confirmLabel="Force Close"
              onConfirm={() => doRunnerAction('force-close')}
              disabled={runnerLoading !== null}
              sizeClassName="px-4 py-2 text-sm"
              idleClassName="bg-orange-800 hover:bg-orange-700 text-white"
            />
          </div>
        </div>
      )}

      {/* Daily Report */}
      {reportStatus && (
        <div className="bg-surface rounded-xl border border-border p-5">
          <div className="flex items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 mb-1">
                <h3 className="text-sm font-semibold text-gray-200">Daily Report</h3>
                <span
                  className={`px-2 py-0.5 rounded text-[10px] font-semibold ${
                    reportStatus.enabled && reportStatus.running
                      ? 'bg-green-900/40 text-green-400'
                      : 'bg-gray-800 text-gray-500'
                  }`}
                >
                  {reportStatus.enabled && reportStatus.running ? 'SCHEDULED' : 'OFF'}
                </span>
              </div>
              <p className="text-xs text-gray-500">
                Sent daily at <span className="text-gray-300 font-mono">{reportStatus.send_time_utc} UTC</span>
                {' '}to <span className="text-gray-300">{reportStatus.recipient}</span>
              </p>
              <p className="text-[11px] text-gray-600 mt-1">
                Last sent: {reportStatus.last_sent_date ?? 'never this session'}
                {reportStatus.last_error && (
                  <span className="text-red-400 ml-2">· error: {reportStatus.last_error}</span>
                )}
              </p>
              {reportToast && (
                <p className="text-[11px] text-cyan-400 mt-2">{reportToast}</p>
              )}
            </div>
            <div className="flex gap-2 shrink-0">
              <button
                onClick={openReportPreview}
                className="px-3 py-1.5 text-xs rounded-lg border border-border hover:bg-background text-gray-300"
              >
                Preview
              </button>
              <button
                onClick={sendReportNow}
                disabled={reportSending}
                className="px-3 py-1.5 text-xs rounded-lg bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 text-white font-medium"
              >
                {reportSending ? 'Sending…' : 'Send Now'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Architecture Note */}
      <div className="bg-surface rounded-xl border border-border/50 p-5">
        <h3 className="text-sm font-semibold text-gray-400 mb-2">Deployment Pipeline</h3>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          <span className="bg-blue-900/30 border border-blue-800/50 px-3 py-1.5 rounded">
            Strategy Dev
          </span>
          <span className="text-gray-600">&rarr;</span>
          <span className="bg-purple-900/30 border border-purple-800/50 px-3 py-1.5 rounded">
            Backtest &amp; Optimize
          </span>
          <span className="text-gray-600">&rarr;</span>
          <span className="bg-yellow-900/30 border border-yellow-800/50 px-3 py-1.5 rounded">
            Paper Trade
          </span>
          <span className="text-gray-600">&rarr;</span>
          <span className="bg-green-900/30 border border-green-800/50 px-3 py-1.5 rounded">
            Cloud Deploy
          </span>
          <span className="text-gray-600">&rarr;</span>
          <span className="bg-cyan-900/30 border border-cyan-800/50 px-3 py-1.5 rounded">
            Monitor &amp; Scale
          </span>
        </div>
      </div>
    </div>
  )
}

// ── Zone Header ─────────────────────────────────────────────

function ZoneHeader({ title, count, dot, hint }: { title: string; count: number; dot: string; hint?: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className={`w-2 h-2 rounded-full ${dot}`} />
      <h3 className="text-sm font-bold uppercase tracking-widest text-gray-300">{title}</h3>
      {hint && <span className="text-xs text-gray-600">({hint})</span>}
      <span className="text-xs text-gray-500">{count}</span>
      <div className="flex-1 h-px bg-border" />
    </div>
  )
}

// ── Confirm Button (two-step destructive action) ────────────

function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  disabled,
  sizeClassName,
  idleClassName,
}: {
  label: string
  confirmLabel?: string
  onConfirm: () => void
  disabled?: boolean
  sizeClassName: string
  idleClassName: string
}) {
  const [armed, setArmed] = useState(false)

  // Auto-revert after 5s if untouched
  useEffect(() => {
    if (!armed) return
    const t = setTimeout(() => setArmed(false), 5000)
    return () => clearTimeout(t)
  }, [armed])

  if (armed) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <button
          onClick={() => {
            setArmed(false)
            onConfirm()
          }}
          className={`${sizeClassName} bg-rose-600 hover:bg-rose-500 text-white font-semibold rounded-lg transition-colors`}
        >
          Confirm {confirmLabel ?? label}?
        </button>
        <button
          onClick={() => setArmed(false)}
          className={`${sizeClassName} bg-gray-800 hover:bg-gray-700 text-gray-400 rounded-lg transition-colors`}
        >
          Cancel
        </button>
      </span>
    )
  }

  return (
    <button
      onClick={() => setArmed(true)}
      disabled={disabled}
      className={`${sizeClassName} ${idleClassName} rounded-lg transition-colors disabled:opacity-50`}
    >
      {label}
    </button>
  )
}

// ── Summary Card ────────────────────────────────────────────

function SummaryCard({ label, value, color }: { label: string; value: string | number; color?: string }) {
  const colorMap: Record<string, string> = {
    green: 'text-green-400',
    red: 'text-red-400',
    yellow: 'text-yellow-400',
    gray: 'text-gray-400',
  }
  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <p className="text-[10px] text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-xl font-mono font-bold mt-1 ${colorMap[color || ''] || 'text-gray-100'}`}>
        {value}
      </p>
    </div>
  )
}

// ── Retirement Details (shared by historical cards) ─────────

function RetirementDetails({ info }: { info: RetirementInfo }) {
  return (
    <div className="mb-4 bg-background rounded-lg p-3 flex items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Retirement Reason</p>
        <p className="text-xs text-gray-400 leading-relaxed">
          {info.reason ?? 'No reason recorded'}
        </p>
      </div>
      {info.finalPnl != null && (
        <div className="shrink-0 text-right">
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Final Realized P&L</p>
          <p className={`text-lg font-mono font-bold ${info.finalPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {formatPnl(info.finalPnl)}
          </p>
        </div>
      )}
    </div>
  )
}

function StageBadge({ stage }: { stage: string }) {
  const cls = stage === 'rejected' ? 'bg-red-500/20 text-red-400' : 'bg-zinc-500/20 text-zinc-400'
  return (
    <span className={`px-2 py-0.5 rounded text-[10px] font-semibold uppercase ${cls}`}>
      {stage}
    </span>
  )
}

// ── Ghost Historical Card (registry-only, deployment record gone) ──

function GhostHistoricalCard({ strategy, info }: { strategy: RegistryStrategyLite; info: RetirementInfo }) {
  return (
    <div className="bg-surface rounded-xl border border-border p-5 opacity-60">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <span className="w-3 h-3 rounded-full flex-shrink-0 bg-zinc-600" />
          <div>
            <h3 className="text-base font-semibold">{strategy.name}</h3>
            <p className="text-xs text-gray-500">
              Strategy: {strategy.id}
              {strategy.links?.deployment && (
                <span className="ml-2 text-gray-600">(deployment record removed: {strategy.links.deployment})</span>
              )}
            </p>
          </div>
        </div>
        <StageBadge stage={info.stage} />
      </div>
      <RetirementDetails info={info} />
    </div>
  )
}

// ── Deployment Card ─────────────────────────────────────────

function DeploymentCard({
  data,
  historical,
  actionLoading,
  onEmergencyStop,
  onForceClose,
  onRestart,
  onToggleLogs,
  logsExpanded,
  logs,
  logsLoading,
}: {
  data: DeploymentStatus
  historical: RetirementInfo | null
  actionLoading: string | null
  onEmergencyStop: () => void
  onForceClose: () => void
  onRestart: () => void
  onToggleLogs: () => void
  logsExpanded: boolean
  logs: string[]
  logsLoading: boolean
}) {
  const dep = data.deployment
  const rs = data.runner_status
  const isRunning = data.online && rs?.running && !rs?.emergency_stopped
  const isEmergency = !!rs?.emergency_stopped
  const snapshot = rs?.position_manager_snapshot

  const regionFlag = regionFlags[dep.region] || ''
  const statusColor = historical
    ? 'bg-zinc-600'
    : isEmergency
    ? 'bg-amber-500'
    : isRunning
    ? 'bg-green-500 animate-pulse'
    : data.online
    ? 'bg-yellow-500'
    : 'bg-gray-600'

  const statusText = isRunning ? 'RUNNING' : data.online ? 'ONLINE (idle)' : 'OFFLINE'
  const statusTextColor = isRunning
    ? 'text-green-400'
    : data.online
    ? 'text-yellow-400'
    : 'text-gray-500'

  return (
    <div
      className={`bg-surface rounded-xl border ${
        !historical && isRunning ? 'border-green-800/40' : 'border-border'
      } p-5 ${historical ? 'opacity-60' : ''}`}
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <span className={`w-3 h-3 rounded-full flex-shrink-0 ${statusColor}`} />
          <div>
            <h3 className="text-base font-semibold">{dep.name}</h3>
            <p className="text-xs text-gray-500">
              {regionFlag && <span className="mr-1">{regionFlag}</span>}
              {dep.region_label} / {dep.exchange.toUpperCase()} / {dep.symbol}
              {dep.asset_class && <span className="ml-2 text-gray-600">({dep.asset_class})</span>}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {data.latency_ms !== null && (
            <span className="text-[10px] text-gray-600 font-mono">{data.latency_ms}ms</span>
          )}
          {historical ? (
            <StageBadge stage={historical.stage} />
          ) : isEmergency ? (
            <span className="px-2 py-0.5 rounded text-[10px] font-semibold bg-amber-500/20 text-amber-400">
              EMERGENCY STOPPED
            </span>
          ) : (
            <span className={`text-sm font-semibold ${statusTextColor}`}>{statusText}</span>
          )}
        </div>
      </div>

      {/* Retirement details */}
      {historical && <RetirementDetails info={historical} />}

      {/* Metrics Grid */}
      {rs && !historical && (
        <div className="grid grid-cols-6 gap-3 mb-4">
          <MetricBox label="Session P&L" value={formatPnl(rs.session_pnl)} color={rs.session_pnl >= 0 ? 'green' : 'red'} />
          <MetricBox label="Trades" value={rs.session_trades} />
          <MetricBox label="Orders" value={rs.session_orders} />
          <MetricBox label="Uptime" value={timeSince(rs.session_start)} />
          <MetricBox
            label="Equity"
            value={snapshot ? `$${snapshot.total_value.toFixed(2)}` : '--'}
          />
          <MetricBox
            label="Positions"
            value={snapshot ? snapshot.num_positions : '--'}
          />
        </div>
      )}

      {/* Open Positions */}
      {!historical && snapshot && snapshot.positions.length > 0 && (
        <div className="mb-4 bg-background rounded-lg p-3">
          <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-2">Open Positions</p>
          {snapshot.positions.map((pos, i) => (
            <div key={i} className="flex items-center justify-between text-sm py-1">
              <span className="font-mono text-gray-300">{pos.symbol}</span>
              <span className={pos.quantity > 0 ? 'text-green-400' : 'text-red-400'}>
                {pos.quantity > 0 ? 'LONG' : 'SHORT'} {Math.abs(pos.quantity).toFixed(4)}
              </span>
              <span className="font-mono text-gray-400">@ ${pos.avg_price.toFixed(2)}</span>
              <span className={`font-mono ${pos.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {formatPnl(pos.unrealized_pnl)}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Server Info */}
      <div className="flex items-center gap-4 text-[10px] text-gray-600 mb-4">
        <span>Host: {dep.host}:{dep.port}</span>
        <span>Strategy: {dep.strategy_id}</span>
        {data.error && <span className="text-red-500">Error: {data.error}</span>}
        {dep.tags.length > 0 && (
          <div className="flex gap-1 ml-auto">
            {dep.tags.map(t => (
              <span key={t} className="bg-background px-1.5 py-0.5 rounded text-[9px] text-gray-500">
                {t}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Controls */}
      <div className="flex items-center gap-2">
        <button
          onClick={onToggleLogs}
          className="px-3 py-1.5 bg-gray-800 hover:bg-gray-700 text-gray-300 text-xs rounded-lg transition-colors"
        >
          {logsExpanded ? 'Hide Logs' : 'View Logs'}
        </button>
        {!historical && (
          <>
            <ConfirmButton
              label="Restart"
              onConfirm={onRestart}
              disabled={actionLoading !== null}
              sizeClassName="px-3 py-1.5 text-xs"
              idleClassName="bg-blue-800 hover:bg-blue-700 text-white"
            />
            <div className="ml-auto flex gap-2">
              <ConfirmButton
                label="Force Close"
                onConfirm={onForceClose}
                disabled={actionLoading !== null || !isRunning}
                sizeClassName="px-3 py-1.5 text-xs"
                idleClassName="bg-orange-800 hover:bg-orange-700 text-white"
              />
              <ConfirmButton
                label="Emergency Stop"
                onConfirm={onEmergencyStop}
                disabled={actionLoading !== null || !isRunning}
                sizeClassName="px-3 py-1.5 text-xs"
                idleClassName="bg-red-800 hover:bg-red-700 text-white"
              />
            </div>
          </>
        )}
      </div>

      {/* Logs Panel */}
      {logsExpanded && (
        <div className="mt-4 bg-background rounded-lg border border-border/50 p-3 max-h-64 overflow-y-auto">
          {logsLoading ? (
            <p className="text-gray-500 text-xs animate-pulse">Loading logs...</p>
          ) : logs.length === 0 ? (
            <p className="text-gray-500 text-xs">No logs available</p>
          ) : (
            <pre className="text-[11px] font-mono text-gray-400 whitespace-pre-wrap leading-relaxed">
              {logs.join('\n')}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

// ── Metric Box ──────────────────────────────────────────────

function MetricBox({ label, value, color }: { label: string; value: string | number; color?: string }) {
  const c = color === 'green' ? 'text-green-400' : color === 'red' ? 'text-red-400' : 'text-gray-100'
  return (
    <div className="bg-background rounded-lg p-2.5">
      <p className="text-[9px] text-gray-500 uppercase tracking-wider">{label}</p>
      <p className={`text-sm font-mono font-bold mt-0.5 ${c}`}>{value}</p>
    </div>
  )
}
