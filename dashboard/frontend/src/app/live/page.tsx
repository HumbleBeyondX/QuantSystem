'use client'

import { useCallback, useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8002'

// ── Types ────────────────────────────────────────────────────

interface StrategyInfo {
  strategy_id: string
  name: string
  state: string
  engine_type: string
  exchange: string | null
  asset_class: string
  allocation: number
  total_pnl: number
  total_trades: number
  error: string | null
  last_heartbeat: string | null
  started_at: string | null
  stopped_at: string | null
}

interface OrchestratorStatus {
  running: boolean
  emergency_stopped: boolean
  strategies: Record<string, StrategyInfo>
}

interface PositionData {
  strategy_id: string
  positions: Record<string, number>
}

interface RunnerStatus {
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
  position_manager_snapshot: any
  message?: string
}

// ── Helpers ──────────────────────────────────────────────────

const stateColors: Record<string, string> = {
  RUNNING: 'bg-green-500',
  PAUSED: 'bg-yellow-500',
  STARTING: 'bg-blue-500 animate-pulse',
  STOPPING: 'bg-orange-500 animate-pulse',
  STOPPED: 'bg-gray-500',
  IDLE: 'bg-gray-600',
  ERROR: 'bg-red-500',
}

const stateTextColors: Record<string, string> = {
  RUNNING: 'text-green-400',
  PAUSED: 'text-yellow-400',
  STARTING: 'text-blue-400',
  STOPPING: 'text-orange-400',
  STOPPED: 'text-gray-400',
  IDLE: 'text-gray-500',
  ERROR: 'text-red-400',
}

function formatPnl(value: number): string {
  const sign = value >= 0 ? '+' : ''
  return `${sign}$${value.toFixed(2)}`
}

function formatTime(iso: string | null): string {
  if (!iso) return '--'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return iso
  }
}

// ── Main Component ──────────────────────────────────────────

export default function LiveTradingPage() {
  const [status, setStatus] = useState<OrchestratorStatus | null>(null)
  const [runnerStatus, setRunnerStatus] = useState<RunnerStatus | null>(null)
  const [positions, setPositions] = useState<Record<string, Record<string, number>>>({})
  const [loading, setLoading] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirmEmergency, setConfirmEmergency] = useState(false)

  // Fetch orchestrator status
  const fetchStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/live/status`)
      if (!res.ok) return
      const data = await res.json()
      setStatus(data)
    } catch {
      // Backend not reachable
    }
  }, [])

  // Fetch standalone runner status
  const fetchRunnerStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/live/runner/status`)
      if (!res.ok) return
      const data = await res.json()
      setRunnerStatus(data)
    } catch {
      // Backend not reachable
    }
  }, [])

  // Fetch positions for a strategy
  const fetchPositions = useCallback(async (strategyId: string) => {
    try {
      const res = await fetch(`${API}/api/live/positions/${strategyId}`)
      if (!res.ok) return
      const data: PositionData = await res.json()
      setPositions(prev => ({ ...prev, [strategyId]: data.positions }))
    } catch {
      // ignore
    }
  }, [])

  // Poll status every 3 seconds
  useEffect(() => {
    fetchStatus()
    fetchRunnerStatus()
    const interval = setInterval(() => {
      fetchStatus()
      fetchRunnerStatus()
    }, 3000)
    return () => clearInterval(interval)
  }, [fetchStatus, fetchRunnerStatus])

  // Fetch positions for running strategies
  useEffect(() => {
    if (!status?.strategies) return
    Object.entries(status.strategies).forEach(([id, info]) => {
      if (info.state === 'RUNNING' || info.state === 'PAUSED') {
        fetchPositions(id)
      }
    })
  }, [status, fetchPositions])

  // Action helpers
  const doAction = async (action: string, strategyId?: string) => {
    setLoading(action)
    setError(null)
    try {
      const url = strategyId
        ? `${API}/api/live/${action}/${strategyId}`
        : `${API}/api/live/${action}`
      const res = await fetch(url, { method: 'POST' })
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }))
        setError(body.detail || body.message || 'Action failed')
      }
      await fetchStatus()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setLoading(null)
    }
  }

  const strategies = status?.strategies ? Object.values(status.strategies) : []

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Live Trading</h1>
          <p className="text-sm text-gray-500 mt-1">
            Orchestrator: {status?.running ? (
              <span className="text-green-400">Active</span>
            ) : (
              <span className="text-gray-500">Inactive</span>
            )}
            {status?.emergency_stopped && (
              <span className="text-red-400 ml-3 font-semibold">EMERGENCY STOP ACTIVE</span>
            )}
          </p>
        </div>

        {/* Emergency Stop */}
        <div className="flex items-center gap-3">
          {status?.emergency_stopped && (
            <button
              onClick={() => doAction('reset-emergency')}
              disabled={loading !== null}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
            >
              Reset Emergency
            </button>
          )}
          {!confirmEmergency ? (
            <button
              onClick={() => setConfirmEmergency(true)}
              className="px-6 py-3 bg-red-700 hover:bg-red-600 text-white font-bold rounded-lg transition-colors border-2 border-red-500"
            >
              EMERGENCY STOP
            </button>
          ) : (
            <div className="flex items-center gap-2">
              <span className="text-red-400 text-sm font-semibold">Confirm?</span>
              <button
                onClick={() => {
                  doAction('emergency-stop')
                  setConfirmEmergency(false)
                }}
                disabled={loading !== null}
                className="px-4 py-2 bg-red-600 hover:bg-red-500 text-white font-bold rounded-lg animate-pulse"
              >
                YES — STOP ALL
              </button>
              <button
                onClick={() => setConfirmEmergency(false)}
                className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-gray-300 rounded-lg text-sm"
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      </div>

      {error && (
        <div className="bg-red-900/30 border border-red-800 rounded-lg p-3 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Standalone Runner Status */}
      {runnerStatus && runnerStatus.running && (
        <div className="bg-surface rounded-xl border border-green-800/50 p-5">
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-3">
              <span className={`w-3 h-3 rounded-full ${runnerStatus.emergency_stopped ? 'bg-red-500' : 'bg-green-500 animate-pulse'}`} />
              <div>
                <h2 className="text-lg font-semibold">
                  {runnerStatus.strategy_id || 'Live Runner'}
                </h2>
                <p className="text-xs text-gray-500">
                  {runnerStatus.exchange?.toUpperCase()} / {runnerStatus.symbol}
                  {runnerStatus.sandbox ? ' (TESTNET)' : ' (MAINNET)'}
                </p>
              </div>
            </div>
            <span className={`text-sm font-semibold ${runnerStatus.emergency_stopped ? 'text-red-400' : 'text-green-400'}`}>
              {runnerStatus.emergency_stopped ? 'EMERGENCY STOPPED' : 'RUNNING'}
            </span>
          </div>

          <div className="grid grid-cols-4 gap-3 mb-4">
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Session P&L</p>
              <p className={`text-lg font-mono font-bold ${runnerStatus.session_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {formatPnl(runnerStatus.session_pnl)}
              </p>
            </div>
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Trades</p>
              <p className="text-lg font-mono font-bold">{runnerStatus.session_trades}</p>
            </div>
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Orders</p>
              <p className="text-lg font-mono font-bold">{runnerStatus.session_orders}</p>
            </div>
            <div className="bg-background rounded-lg p-3">
              <p className="text-[10px] text-gray-500 uppercase tracking-wider">Started</p>
              <p className="text-sm font-mono text-gray-300">
                {runnerStatus.session_start ? formatTime(runnerStatus.session_start) : '--'}
              </p>
            </div>
          </div>

          <div className="flex gap-2">
            <button
              onClick={() => doAction('runner/emergency-stop')}
              disabled={loading !== null || runnerStatus.emergency_stopped}
              className="px-4 py-2 bg-red-800 hover:bg-red-700 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
            >
              Emergency Stop
            </button>
            <button
              onClick={() => doAction('runner/force-close')}
              disabled={loading !== null}
              className="px-4 py-2 bg-orange-800 hover:bg-orange-700 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
            >
              Force Close Positions
            </button>
          </div>
        </div>
      )}

      {/* Strategy Cards */}
      {strategies.length === 0 && (!runnerStatus || !runnerStatus.running) ? (
        <div className="bg-surface rounded-xl border border-border p-12 text-center">
          <p className="text-gray-500 text-lg">No strategies registered</p>
          <p className="text-gray-600 text-sm mt-2">
            Start with: QS_LIVE_CONFIG=config/live/eth_sniper_live.yaml uvicorn dashboard.backend.main:app --port 8002
          </p>
        </div>
      ) : strategies.length > 0 ? (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-5">
          {strategies.map(s => (
            <StrategyCard
              key={s.strategy_id}
              strategy={s}
              positions={positions[s.strategy_id] || {}}
              loading={loading}
              onStart={() => doAction('start', s.strategy_id)}
              onStop={() => doAction('stop', s.strategy_id)}
              onPause={() => doAction('pause', s.strategy_id)}
              onResume={() => doAction('resume', s.strategy_id)}
              onForceClose={() => doAction('force-close', s.strategy_id)}
            />
          ))}
        </div>
      ) : null}

      {/* Active Positions Summary */}
      {Object.keys(positions).length > 0 && (
        <div className="bg-surface rounded-xl border border-border p-5">
          <h2 className="text-lg font-semibold mb-4">Open Positions</h2>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-500 border-b border-border">
                  <th className="text-left py-2 px-3">Strategy</th>
                  <th className="text-left py-2 px-3">Symbol</th>
                  <th className="text-right py-2 px-3">Quantity</th>
                  <th className="text-center py-2 px-3">Side</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(positions).flatMap(([sid, posMap]) =>
                  Object.entries(posMap)
                    .filter(([_, qty]) => Math.abs(qty) > 1e-10)
                    .map(([symbol, qty]) => (
                      <tr key={`${sid}_${symbol}`} className="border-b border-border/50">
                        <td className="py-2 px-3 text-gray-400">{sid}</td>
                        <td className="py-2 px-3 font-mono">{symbol}</td>
                        <td className="py-2 px-3 text-right font-mono">{Math.abs(qty).toFixed(4)}</td>
                        <td className="py-2 px-3 text-center">
                          <span className={qty > 0 ? 'text-green-400' : 'text-red-400'}>
                            {qty > 0 ? 'LONG' : 'SHORT'}
                          </span>
                        </td>
                      </tr>
                    ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Strategy Card Component ──────────────────────────────────

function StrategyCard({
  strategy: s,
  positions,
  loading,
  onStart,
  onStop,
  onPause,
  onResume,
  onForceClose,
}: {
  strategy: StrategyInfo
  positions: Record<string, number>
  loading: string | null
  onStart: () => void
  onStop: () => void
  onPause: () => void
  onResume: () => void
  onForceClose: () => void
}) {
  const isActive = s.state === 'RUNNING' || s.state === 'PAUSED'
  const posCount = Object.values(positions).filter(q => Math.abs(q) > 1e-10).length

  return (
    <div className="bg-surface rounded-xl border border-border p-5">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <span className={`w-3 h-3 rounded-full ${stateColors[s.state] || 'bg-gray-600'}`} />
          <div>
            <h3 className="text-base font-semibold">{s.name || s.strategy_id}</h3>
            <p className="text-xs text-gray-500">
              {s.engine_type.toUpperCase()}
              {s.exchange && ` / ${s.exchange.toUpperCase()}`}
              {' / '}
              {s.asset_class}
            </p>
          </div>
        </div>
        <span className={`text-sm font-semibold ${stateTextColors[s.state] || 'text-gray-500'}`}>
          {s.state}
        </span>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div className="bg-background rounded-lg p-3">
          <p className="text-[10px] text-gray-500 uppercase tracking-wider">Total P&L</p>
          <p className={`text-lg font-mono font-bold ${s.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {formatPnl(s.total_pnl)}
          </p>
        </div>
        <div className="bg-background rounded-lg p-3">
          <p className="text-[10px] text-gray-500 uppercase tracking-wider">Trades</p>
          <p className="text-lg font-mono font-bold">{s.total_trades}</p>
        </div>
        <div className="bg-background rounded-lg p-3">
          <p className="text-[10px] text-gray-500 uppercase tracking-wider">Positions</p>
          <p className="text-lg font-mono font-bold">{posCount}</p>
        </div>
      </div>

      {/* Error */}
      {s.error && (
        <div className="bg-red-900/20 border border-red-800/50 rounded-lg p-2 mb-4 text-xs text-red-300">
          {s.error}
        </div>
      )}

      {/* Timestamps */}
      <div className="text-[10px] text-gray-600 mb-4 space-y-0.5">
        {s.started_at && <p>Started: {formatTime(s.started_at)}</p>}
        {s.stopped_at && <p>Stopped: {formatTime(s.stopped_at)}</p>}
        {s.last_heartbeat && <p>Heartbeat: {formatTime(s.last_heartbeat)}</p>}
      </div>

      {/* Controls */}
      <div className="flex flex-wrap gap-2">
        {s.state === 'IDLE' || s.state === 'STOPPED' || s.state === 'ERROR' ? (
          <button
            onClick={onStart}
            disabled={loading !== null}
            className="px-4 py-2 bg-green-700 hover:bg-green-600 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
          >
            Start
          </button>
        ) : null}

        {s.state === 'RUNNING' ? (
          <>
            <button
              onClick={onPause}
              disabled={loading !== null}
              className="px-4 py-2 bg-yellow-700 hover:bg-yellow-600 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
            >
              Pause
            </button>
            <button
              onClick={onStop}
              disabled={loading !== null}
              className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
            >
              Stop
            </button>
          </>
        ) : null}

        {s.state === 'PAUSED' ? (
          <>
            <button
              onClick={onResume}
              disabled={loading !== null}
              className="px-4 py-2 bg-green-700 hover:bg-green-600 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
            >
              Resume
            </button>
            <button
              onClick={onStop}
              disabled={loading !== null}
              className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white text-sm rounded-lg transition-colors disabled:opacity-50"
            >
              Stop
            </button>
          </>
        ) : null}

        {isActive && (
          <button
            onClick={onForceClose}
            disabled={loading !== null}
            className="px-4 py-2 bg-red-800 hover:bg-red-700 text-white text-sm rounded-lg transition-colors disabled:opacity-50 ml-auto"
          >
            Force Close
          </button>
        )}
      </div>
    </div>
  )
}
