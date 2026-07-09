'use client'

/**
 * Strategy Monitor — real data only.
 *
 * Renders /api/monitor/strategies: every effective strategy as a card with
 * portfolio rollup, expectancy flags and REAL equity sparklines. Cards for
 * unreachable sources degrade visibly (never silently show stale numbers).
 * Auto-refreshes every 15s.
 */

import { useCallback, useEffect, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'
const REFRESH_MS = 15_000

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
  running: '运行中', flat: '空仓等待', stale: '数据陈旧', unreachable: '失联',
}

function Spark({ data }: { data: number[] | null }) {
  if (!data || data.length < 2)
    return (
      <div className="w-32 h-10 flex items-center justify-center text-[10px] text-gray-600 border border-dashed border-gray-700 rounded">
        无历史数据
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
      <polygon
        points={`2,38 ${pts} 126,38`}
        fill={col}
        opacity="0.12"
      />
      <circle cx={lx} cy={ly} r="2.5" fill={col} />
    </svg>
  )
}

function StrategyCard({ c }: { c: Card }) {
  const flagged = c.flags.includes('thin_edge')
  return (
    <div
      className={`bg-surface border border-border rounded-lg p-4 hover:border-gray-600 transition-colors ${
        flagged ? 'border-l-4 border-l-amber-500' : ''
      } ${c.status === 'unreachable' ? 'opacity-70' : ''}`}
    >
      <div className="flex items-center gap-2">
        <span className="font-mono font-bold text-[15px]">{c.id}</span>
        <span className="text-xs text-gray-400 truncate flex-1" title={c.name}>
          {c.name}
        </span>
        <span className="text-[10px] px-2 py-0.5 rounded bg-gray-700/40 text-gray-300 uppercase">
          {c.mode}
        </span>
        <span
          className={`text-[10px] px-2 py-0.5 rounded font-semibold ${
            STATUS_STYLE[c.status] ?? ''
          }`}
        >
          {STATUS_LABEL[c.status] ?? c.status}
        </span>
      </div>

      {c.status === 'unreachable' ? (
        <div className="mt-4 text-sm text-gray-500">
          数据源不可达(<span className="font-mono">{c.source}</span>)——
          不显示过期数字。恢复连接后自动更新。
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
                今日已实现 {fmt$(c.today_realized)} · 未实现 {fmt$(c.unrealized)}
              </div>
            </div>
            <Spark data={c.spark} />
          </div>

          <div className="grid grid-cols-3 gap-x-3 gap-y-2 mt-3 pt-3 border-t border-border text-[13px]">
            <div>
              <div className="text-[10px] text-gray-600">总收益</div>
              <div className={`font-mono font-semibold ${pnlColor(c.total_return_pct)}`}>
                {c.total_return_pct == null
                  ? '—'
                  : `${c.total_return_pct >= 0 ? '+' : ''}${c.total_return_pct.toFixed(2)}%`}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">权益</div>
              <div className="font-mono font-semibold">
                {c.equity == null ? '—' : `$${c.equity.toLocaleString('en-US', { maximumFractionDigits: 0 })}`}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">持仓</div>
              <div className="font-mono font-semibold">{c.positions ?? '—'}</div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">胜率 × 盈亏比</div>
              <div className="font-mono font-semibold">
                {c.win_rate != null ? `${c.win_rate}%` : '—'}
                {c.profit_factor != null ? ` × ${c.profit_factor}` : ''}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">期望值</div>
              <div
                className={`font-mono font-semibold ${
                  c.expectancy == null
                    ? 'text-gray-500'
                    : c.expectancy < 0.03
                      ? 'text-amber-400'
                      : 'text-emerald-400'
                }`}
              >
                {c.expectancy == null ? '样本不足' : c.expectancy.toFixed(3)}
              </div>
            </div>
            <div>
              <div className="text-[10px] text-gray-600">交易数</div>
              <div className="font-mono font-semibold">{c.trades ?? '—'}</div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}

export default function MonitorPage() {
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [lastOk, setLastOk] = useState<Date | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/monitor/strategies`, { cache: 'no-store' })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setSnap(await r.json())
      setErr(null)
      setLastOk(new Date())
    } catch (e) {
      setErr(String(e))
    }
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, REFRESH_MS)
    return () => clearInterval(t)
  }, [load])

  const flagged = snap?.strategies.filter((s) => s.flags.includes('thin_edge')) ?? []
  const unreachable = snap?.strategies.filter((s) => s.status === 'unreachable') ?? []

  return (
    <div className="p-6 space-y-4">
      <div className="flex items-baseline gap-3 flex-wrap">
        <h1 className="text-xl font-bold">策略监控</h1>
        <span className="text-xs text-gray-500">
          {err
            ? `⚠ 后端不可达 (${err}) — 显示的是 ${lastOk ? lastOk.toLocaleTimeString() : '无'} 的数据`
            : lastOk
              ? `实时 · 每 ${REFRESH_MS / 1000}s 刷新 · 更新于 ${lastOk.toLocaleTimeString()}`
              : '加载中…'}
        </span>
      </div>

      {snap && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {[
            ['总权益', `$${snap.total_equity.toLocaleString('en-US', { maximumFractionDigits: 0 })}`, ''],
            ['今日已实现', fmt$(snap.today_realized), pnlColor(snap.today_realized)],
            ['未实现', fmt$(snap.unrealized), pnlColor(snap.unrealized)],
            ['持仓数', String(snap.open_positions), ''],
          ].map(([l, v, cls]) => (
            <div key={l as string} className="bg-surface border border-border rounded-lg p-3">
              <div className="text-[10px] text-gray-500 uppercase tracking-wider">{l}</div>
              <div className={`text-xl font-bold font-mono mt-1 ${cls}`}>{v}</div>
            </div>
          ))}
        </div>
      )}

      {(flagged.length > 0 || unreachable.length > 0) && (
        <div className="border border-amber-500/40 bg-amber-500/5 rounded-lg px-4 py-2.5 text-sm space-x-3">
          {flagged.length > 0 && (
            <span>
              <b className="text-amber-400">⚠ 期望值偏薄:</b>{' '}
              {flagged.map((s) => `${s.id} (${s.expectancy?.toFixed(3)})`).join('、')}
            </span>
          )}
          {unreachable.length > 0 && (
            <span>
              <b className="text-rose-400">✕ 失联:</b>{' '}
              {unreachable.map((s) => s.id).join('、')}
            </span>
          )}
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-3 gap-3">
        {snap
          ? snap.strategies.map((c) => <StrategyCard key={c.id} c={c} />)
          : !err && (
              <div className="text-gray-500 text-sm p-8">加载策略数据…</div>
            )}
      </div>

      <p className="text-[11px] text-gray-600 max-w-2xl">
        期望值 = 胜率 × 盈亏比 − (1 − 胜率),阈值 0.03。曲线为真实权益历史
        (paper 日志 / 交易所快照),失联的数据源明确标注而非展示过期数字。
      </p>
    </div>
  )
}
