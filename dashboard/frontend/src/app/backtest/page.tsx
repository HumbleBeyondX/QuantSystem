'use client'

import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'next/navigation'
import Link from 'next/link'
import TearSheet from '@/components/charts/TearSheet'

/**
 * Backtest Page - Fully API-driven with workflow integration
 *
 * Features:
 * - Dynamic strategy catalog from /api/strategies/catalog
 * - URL query param pre-population (?strategy=X&symbol=Y)
 * - Run backtests via POST /api/backtest/run
 * - Poll status until completion
 * - Display results with equity curve, metrics, trades
 * - List past backtest results from /api/backtest/results
 * - Strategy parameter editing from catalog defaults
 */

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'

interface StrategyInfo {
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
  runnable?: boolean
}

interface BacktestResult {
  backtest_id: string
  strategy: string
  symbol: string
  status: string
  created_at: string
  initial_capital?: number
  parameters?: Record<string, any>
  metrics: Record<string, any>
  n_trades: number
  final_value: number
  equity_curve?: Array<Record<string, any>>
  trades?: Array<Record<string, any>>
  error?: string
}

// Inner component that uses useSearchParams (must be inside Suspense)
function BacktestInner() {
  const searchParams = useSearchParams()
  const qStrategy = searchParams.get('strategy')
  const qSymbol = searchParams.get('symbol')

  const [catalog, setCatalog] = useState<StrategyInfo[]>([])
  const [selectedStrategy, setSelectedStrategy] = useState('')
  const [symbol, setSymbol] = useState('BTC/USDT')
  const [startDate, setStartDate] = useState('2023-01-01')
  const [endDate, setEndDate] = useState('2026-04-01')
  const [initialCapital, setInitialCapital] = useState('100000')
  const [params, setParams] = useState<Record<string, any>>({})
  const [showParams, setShowParams] = useState(false)
  const [isRunning, setIsRunning] = useState(false)
  const [runningId, setRunningId] = useState<string | null>(null)
  const [results, setResults] = useState<BacktestResult[]>([])
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [selectedResult, setSelectedResult] = useState<BacktestResult | null>(null)
  const [dataFiles, setDataFiles] = useState<Array<{ filename: string; rows: number; size_mb: number }>>([])
  const [selectedFile, setSelectedFile] = useState('')
  const [statusMsg, setStatusMsg] = useState('')
  const [catalogLoaded, setCatalogLoaded] = useState(false)
  const [recordsRefresh, setRecordsRefresh] = useState(0)

  // Fetch catalog, results, data files on mount
  useEffect(() => {
    fetchCatalog()
    fetchResults()
    fetchDataFiles()
  }, [])

  const fetchCatalog = async () => {
    try {
      const res = await fetch(`${API}/api/strategies/catalog`)
      if (res.ok) {
        const data: StrategyInfo[] = await res.json()
        setCatalog(data)
        // Apply query params or fallback to first strategy
        if (qStrategy) {
          setSelectedStrategy(qStrategy)
          const match = data.find(s => s.id === qStrategy)
          if (match) {
            setSymbol(qSymbol || match.symbols[0] || 'BTC/USDT')
            setParams(match.default_params || {})
            // Set appropriate capital based on asset class
            if (match.asset_class === 'crypto') {
              setInitialCapital('100000')
            } else if (match.asset_class === 'options') {
              setInitialCapital('500000')
            } else {
              setInitialCapital('250000')
            }
          } else {
            setSymbol(qSymbol || 'BTC/USDT')
          }
        } else {
          const runnables = data.filter(s => s.runnable === true)
          const first = runnables[0] || null
          if (first) {
            setSelectedStrategy(first.id)
            setSymbol(first.symbols[0] || 'BTC/USDT')
            setParams(first.default_params || {})
          }
        }
        setCatalogLoaded(true)
      }
    } catch {
      setCatalogLoaded(true)
    }
  }

  const fetchResults = async () => {
    try {
      const res = await fetch(`${API}/api/backtest/results`)
      if (res.ok) {
        const data = await res.json()
        setResults(data)
      }
    } catch {}
  }

  const fetchDataFiles = async () => {
    try {
      const res = await fetch(`${API}/api/backtest/data-files`)
      if (res.ok) {
        const data = await res.json()
        setDataFiles(data)
        if (data.length > 0) setSelectedFile(data[0].filename)
      }
    } catch {}
  }

  // When strategy selection changes, update symbol + params from catalog
  const handleStrategyChange = (strategyId: string) => {
    setSelectedStrategy(strategyId)
    const match = catalog.find(s => s.id === strategyId)
    if (match) {
      setSymbol(match.symbols[0] || symbol)
      setParams(match.default_params || {})
    }
  }

  // Edit a param value
  const updateParam = (key: string, value: string) => {
    const numVal = parseFloat(value)
    setParams(prev => ({
      ...prev,
      [key]: isNaN(numVal) ? value : numVal,
    }))
  }

  // Poll status of a running backtest
  const pollStatus = useCallback(async (backtestId: string) => {
    const maxAttempts = 300
    for (let i = 0; i < maxAttempts; i++) {
      await new Promise(r => setTimeout(r, 1000))
      try {
        const res = await fetch(`${API}/api/backtest/status/${backtestId}`)
        if (!res.ok) continue
        const data = await res.json()
        setStatusMsg(`Status: ${data.status} (${i + 1}s)`)

        if (data.status === 'completed' || data.status === 'failed') {
          const fullRes = await fetch(`${API}/api/backtest/results/${backtestId}`)
          if (fullRes.ok) {
            const fullData = await fullRes.json()
            setSelectedResult(fullData)
            setExpandedId(backtestId)
          }
          await fetchResults()
          setIsRunning(false)
          setRunningId(null)
          setStatusMsg(data.status === 'completed' ? 'Backtest completed!' : `Failed: ${data.error || 'unknown'}`)
          return
        }
      } catch {}
    }
    setIsRunning(false)
    setStatusMsg('Timeout waiting for backtest')
  }, [])

  const handleRun = async () => {
    setIsRunning(true)
    setStatusMsg('Submitting backtest...')
    setSelectedResult(null)

    try {
      const body: any = {
        strategy: selectedStrategy,
        symbol,
        initial_capital: parseFloat(initialCapital),
        start_date: startDate || undefined,
        end_date: endDate || undefined,
      }
      if (selectedFile) body.data_file = selectedFile
      // Include params if user has modified them
      if (Object.keys(params).length > 0) body.parameters = params

      const res = await fetch(`${API}/api/backtest/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })

      if (!res.ok) {
        const errText = await res.text().catch(() => `Status ${res.status}`)
        setStatusMsg(`Error: ${errText}`)
        setIsRunning(false)
        return
      }

      const data = await res.json()
      setRunningId(data.backtest_id)
      setStatusMsg(`Queued: ${data.backtest_id}`)
      pollStatus(data.backtest_id)
    } catch (e: any) {
      setStatusMsg(`Error: ${e.message}`)
      setIsRunning(false)
    }
  }

  const loadDetail = async (backtestId: string) => {
    if (expandedId === backtestId) {
      setExpandedId(null)
      setSelectedResult(null)
      return
    }
    try {
      const res = await fetch(`${API}/api/backtest/results/${backtestId}`)
      if (res.ok) {
        const data = await res.json()
        setSelectedResult(data)
        setExpandedId(backtestId)
      }
    } catch {}
  }

  // Get current strategy info
  const currentStrategy = catalog.find(s => s.id === selectedStrategy)
  const m = selectedResult?.metrics || {}

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Backtest</h2>
          {qStrategy && currentStrategy && (
            <p className="text-sm text-gray-400 mt-1">
              Pre-configured for <span className="text-accent font-medium">{currentStrategy.name}</span>
            </p>
          )}
        </div>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          <span>{catalog.length} strategies available</span>
          <span className="w-1 h-1 rounded-full bg-gray-600" />
          <span>{results.length} past runs</span>
        </div>
      </div>

      {/* Research pipeline map */}
      <PipelineStrip />

      {/* Runtime v2 Workbench — button-driven research loop */}
      <RuntimeV2Workbench />

      {/* Official Research Records */}
      <OfficialRecords refreshKey={recordsRefresh} />

      {/* Configuration Form */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <h3 className="text-lg font-semibold mb-4">Configuration</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {/* Strategy Selector */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Strategy</label>
            {!catalogLoaded ? (
              <div className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-500 animate-pulse">
                Loading strategies...
              </div>
            ) : (
              <select
                value={selectedStrategy}
                onChange={e => handleStrategyChange(e.target.value)}
                className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-accent"
              >
                {/* Group by asset class — runnable strategies only */}
                {['crypto', 'options', 'equity'].map(cls => {
                  const group = catalog.filter(s => s.asset_class === cls && s.runnable === true)
                  if (group.length === 0) return null
                  return (
                    <optgroup key={cls} label={cls.charAt(0).toUpperCase() + cls.slice(1)}>
                      {group.map(s => (
                        <option key={s.id} value={s.id}>{s.name}</option>
                      ))}
                    </optgroup>
                  )
                })}
              </select>
            )}
          </div>

          {/* Symbol */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Symbol</label>
            <input
              type="text"
              value={symbol}
              onChange={e => setSymbol(e.target.value)}
              placeholder="BTC/USDT"
              className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-accent"
            />
            {currentStrategy && currentStrategy.symbols.length > 1 && (
              <div className="flex flex-wrap gap-1 mt-1.5">
                {currentStrategy.symbols.map(s => (
                  <button
                    key={s}
                    onClick={() => setSymbol(s)}
                    className={`px-2 py-0.5 rounded text-[10px] border transition-colors ${
                      symbol === s
                        ? 'border-accent text-accent bg-accent/10'
                        : 'border-border text-gray-500 hover:text-gray-300'
                    }`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* Data File */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Data File</label>
            <select
              value={selectedFile}
              onChange={e => setSelectedFile(e.target.value)}
              className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-accent"
            >
              <option value="">Auto-detect</option>
              {dataFiles.map(f => (
                <option key={f.filename} value={f.filename}>
                  {f.filename} ({f.rows.toLocaleString()} rows, {f.size_mb}MB)
                </option>
              ))}
            </select>
          </div>

          {/* Start Date */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Start Date</label>
            <input
              type="date"
              value={startDate}
              onChange={e => setStartDate(e.target.value)}
              className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-accent"
            />
          </div>

          {/* End Date */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">End Date</label>
            <input
              type="date"
              value={endDate}
              onChange={e => setEndDate(e.target.value)}
              className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-accent"
            />
          </div>

          {/* Initial Capital */}
          <div>
            <label className="block text-sm text-gray-400 mb-1.5">Initial Capital ($)</label>
            <input
              type="number"
              value={initialCapital}
              onChange={e => setInitialCapital(e.target.value)}
              className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 focus:outline-none focus:border-accent"
            />
          </div>
        </div>

        {/* Strategy Parameters (collapsible) */}
        {Object.keys(params).length > 0 && (
          <div className="mt-4">
            <button
              onClick={() => setShowParams(!showParams)}
              className="flex items-center gap-2 text-sm text-gray-400 hover:text-gray-200 transition-colors"
            >
              <span className={`transition-transform ${showParams ? 'rotate-90' : ''}`}>&#9654;</span>
              Strategy Parameters ({Object.keys(params).length})
            </button>
            {showParams && (
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3 mt-3 p-4 bg-background rounded-lg border border-border">
                {Object.entries(params).map(([key, val]) => (
                  <div key={key}>
                    <label className="block text-[11px] text-gray-500 mb-1 font-mono">{key}</label>
                    <input
                      type="text"
                      value={String(val)}
                      onChange={e => updateParam(key, e.target.value)}
                      className="w-full bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent font-mono"
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Strategy Info Banner */}
        {currentStrategy && (
          <div className="mt-4 p-3 rounded-lg bg-background border border-border/50">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${
                  currentStrategy.asset_class === 'crypto' ? 'bg-warning/10 text-warning' :
                  currentStrategy.asset_class === 'options' ? 'bg-accent/10 text-accent' :
                  'bg-success/10 text-success'
                }`}>
                  {currentStrategy.asset_class}
                </span>
                <span className="text-xs text-gray-400">{currentStrategy.description.substring(0, 120)}...</span>
              </div>
              {currentStrategy.backtest_sharpe !== null && (
                <div className="flex items-center gap-4 text-xs text-gray-500 flex-shrink-0 ml-4">
                  <span>Sharpe: <span className="text-gray-300">{currentStrategy.backtest_sharpe?.toFixed(2)}</span></span>
                  <span>WR: <span className="text-gray-300">{currentStrategy.backtest_win_rate !== null ? `${(currentStrategy.backtest_win_rate > 1 ? currentStrategy.backtest_win_rate : currentStrategy.backtest_win_rate * 100).toFixed(0)}%` : 'N/A'}</span></span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Run Button + Status */}
        <div className="mt-4 flex items-center justify-between">
          <span className="text-sm text-gray-500">{statusMsg}</span>
          <button
            onClick={handleRun}
            disabled={isRunning || !selectedStrategy}
            className={`px-6 py-2.5 rounded-lg text-sm font-medium transition-colors ${
              isRunning || !selectedStrategy
                ? 'bg-accent/50 text-white/50 cursor-not-allowed'
                : 'bg-accent text-white hover:bg-accent/80'
            }`}
          >
            {isRunning ? (
              <span className="flex items-center gap-2">
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
                Running...
              </span>
            ) : 'Run Backtest'}
          </button>
        </div>
      </div>

      {/* Active Result Detail */}
      {selectedResult && selectedResult.status === 'completed' && (
        <div className="bg-surface rounded-xl border border-border p-6 space-y-6">
          <div className="flex items-center justify-between">
            <h3 className="text-lg font-semibold">
              Result: {selectedResult.strategy} on {selectedResult.symbol}
            </h3>
            <span className="px-2 py-0.5 rounded text-xs font-medium bg-success/10 text-success">
              {selectedResult.status}
            </span>
          </div>

          {/* Promote to official record */}
          <PromotePanel
            key={selectedResult.backtest_id}
            result={selectedResult}
            onPromoted={() => setRecordsRefresh(k => k + 1)}
          />

          {/* KPI Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
            {[
              { label: 'Total Return', value: `${(m.total_return_pct ?? 0).toFixed(2)}%`, color: (m.total_return_pct ?? 0) >= 0 ? 'text-success' : 'text-danger' },
              { label: 'Sharpe Ratio', value: (m.sharpe_ratio ?? 0).toFixed(3), color: 'text-accent' },
              { label: 'Sortino', value: (m.sortino_ratio ?? 0).toFixed(3), color: 'text-gray-100' },
              { label: 'Max Drawdown', value: `${(m.max_drawdown_pct ?? 0).toFixed(2)}%`, color: 'text-danger' },
              { label: 'Win Rate', value: `${((m.win_rate ?? 0) * 100).toFixed(1)}%`, color: 'text-gray-100' },
              { label: 'Profit Factor', value: (m.profit_factor ?? 0).toFixed(2), color: 'text-gray-100' },
              { label: 'Calmar', value: (m.calmar_ratio ?? 0).toFixed(3), color: 'text-gray-100' },
              { label: 'Annual Vol', value: `${((m.annual_volatility ?? 0) * 100).toFixed(1)}%`, color: 'text-warning' },
              { label: 'Trades', value: selectedResult.n_trades.toString(), color: 'text-gray-100' },
              { label: 'Final Value', value: `$${(selectedResult.final_value ?? 0).toLocaleString(undefined, { maximumFractionDigits: 0 })}`, color: 'text-accent' },
              { label: 'Net P&L', value: `$${((selectedResult.final_value ?? 0) - (selectedResult.initial_capital ?? 100000)).toLocaleString(undefined, { maximumFractionDigits: 0 })}`, color: (selectedResult.final_value ?? 0) >= (selectedResult.initial_capital ?? 100000) ? 'text-success' : 'text-danger' },
              { label: 'Avg Win/Loss', value: (m.win_loss_ratio ?? 0).toFixed(2), color: 'text-gray-100' },
            ].map((kpi, i) => (
              <div key={i} className="bg-background rounded-lg p-3">
                <p className="text-xs text-gray-500 mb-1">{kpi.label}</p>
                <p className={`text-sm font-bold ${kpi.color}`}>{kpi.value}</p>
              </div>
            ))}
          </div>

          {/* Equity Curve */}
          {selectedResult.equity_curve && selectedResult.equity_curve.length > 1 && (
            <div>
              <h4 className="text-sm font-semibold text-gray-400 mb-3">Equity Curve</h4>
              <div className="h-48 relative">
                <EquityCurveSVG data={selectedResult.equity_curve} initialCash={selectedResult.initial_capital ?? 100000} />
              </div>
            </div>
          )}

          {/* Tear sheet — full analytics vs benchmark */}
          <div className="pt-2 border-t border-border/50">
            <TearSheet
              key={selectedResult.backtest_id}
              analyticsUrl={`/api/backtest/results/${selectedResult.backtest_id}/analytics`}
            />
          </div>

          {/* Recent Trades */}
          {selectedResult.trades && selectedResult.trades.length > 0 && (
            <div>
              <h4 className="text-sm font-semibold text-gray-400 mb-3">
                Recent Trades ({Math.min(selectedResult.trades.length, 20)} of {selectedResult.trades.length})
              </h4>
              <div className="overflow-x-auto max-h-64 overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface">
                    <tr className="text-gray-500 border-b border-border">
                      <th className="text-left pb-2 pr-4">Time</th>
                      <th className="text-left pb-2 pr-4">Symbol</th>
                      <th className="text-left pb-2 pr-4">Side</th>
                      <th className="text-right pb-2 pr-4">Price</th>
                      <th className="text-right pb-2 pr-4">Qty</th>
                      <th className="text-right pb-2">Fee</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedResult.trades.slice(-20).map((t: any, i: number) => {
                      const time = (t.timestamp || t.ts || '').substring(0, 19)
                      const sym = t.symbol || selectedResult.symbol || ''
                      const side = t.side || t.action || ''
                      const qty = t.quantity ?? t.qty ?? 0
                      const fee = t.commission ?? t.fee ?? 0
                      const isBuy = /buy|long/i.test(side)
                      return (
                        <tr key={i} className="border-b border-border/30">
                          <td className="py-1.5 pr-4 text-gray-400">{time}</td>
                          <td className="py-1.5 pr-4 text-gray-100">{sym}</td>
                          <td className={`py-1.5 pr-4 font-medium ${isBuy ? 'text-success' : 'text-danger'}`}>
                            {side}
                          </td>
                          <td className="py-1.5 pr-4 text-right text-gray-100">${(t.price ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
                          <td className="py-1.5 pr-4 text-right text-gray-300">{qty.toFixed(6)}</td>
                          <td className="py-1.5 text-right text-gray-500">${fee.toFixed(4)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Error state */}
      {selectedResult && selectedResult.status === 'failed' && (
        <div className="bg-surface rounded-xl border border-danger/30 p-6">
          <h3 className="text-lg font-semibold text-danger mb-2">Backtest Failed</h3>
          <p className="text-sm text-gray-400">{selectedResult.error}</p>
        </div>
      )}

      {/* Past Backtests */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">Past Backtests</h3>
          <button onClick={fetchResults} className="text-xs text-accent hover:underline">Refresh</button>
        </div>
        {results.length === 0 ? (
          <p className="text-sm text-gray-500 py-8 text-center">
            No backtests yet. Configure a strategy above and click &quot;Run Backtest&quot;.
            <br />
            <code className="text-accent text-xs mt-2 block">
              python scripts/run_backtest.py --strategy crypto_scalping --data-path data/sample/BTC_USDT_1m.csv
            </code>
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-400 border-b border-border">
                  <th className="text-left pb-3 font-medium">ID</th>
                  <th className="text-left pb-3 font-medium">Strategy</th>
                  <th className="text-left pb-3 font-medium">Symbol</th>
                  <th className="text-right pb-3 font-medium">Return</th>
                  <th className="text-right pb-3 font-medium">Sharpe</th>
                  <th className="text-right pb-3 font-medium">Max DD</th>
                  <th className="text-right pb-3 font-medium">Trades</th>
                  <th className="text-center pb-3 font-medium">Status</th>
                  <th className="text-left pb-3 font-medium">Time</th>
                </tr>
              </thead>
              <tbody>
                {results.map(bt => {
                  const ret = bt.metrics?.total_return_pct ?? 0
                  const sharpe = bt.metrics?.sharpe_ratio ?? 0
                  const dd = bt.metrics?.max_drawdown_pct ?? 0
                  return (
                    <tr
                      key={bt.backtest_id}
                      className="border-b border-border/50 hover:bg-background/50 transition-colors cursor-pointer"
                      onClick={() => loadDetail(bt.backtest_id)}
                    >
                      <td className="py-2.5 text-xs text-gray-500 font-mono">{bt.backtest_id.substring(0, 8)}</td>
                      <td className="py-2.5 font-medium text-gray-100">{bt.strategy}</td>
                      <td className="py-2.5 text-gray-400">{bt.symbol}</td>
                      <td className={`py-2.5 text-right font-medium ${ret >= 0 ? 'text-success' : 'text-danger'}`}>
                        {ret >= 0 ? '+' : ''}{ret.toFixed(2)}%
                      </td>
                      <td className="py-2.5 text-right text-gray-100">{sharpe.toFixed(2)}</td>
                      <td className="py-2.5 text-right text-danger">{dd.toFixed(1)}%</td>
                      <td className="py-2.5 text-right text-gray-400">{bt.n_trades}</td>
                      <td className="py-2.5 text-center">
                        <span className={`px-2 py-0.5 rounded text-xs font-medium ${
                          bt.status === 'completed' ? 'bg-success/10 text-success' :
                          bt.status === 'running' ? 'bg-warning/10 text-warning' :
                          bt.status === 'failed' ? 'bg-danger/10 text-danger' :
                          'bg-gray-500/10 text-gray-500'
                        }`}>
                          {bt.status}
                        </span>
                      </td>
                      <td className="py-2.5 text-xs text-gray-500">
                        {bt.created_at ? bt.created_at.substring(0, 16).replace('T', ' ') : ''}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// Main export with Suspense wrapper (required for useSearchParams)
export default function BacktestPage() {
  return (
    <Suspense fallback={
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Backtest</h2>
        <div className="bg-surface rounded-xl border border-border p-6 animate-pulse">
          <div className="h-6 w-40 bg-background rounded mb-4" />
          <div className="grid grid-cols-3 gap-4">
            <div className="h-10 bg-background rounded" />
            <div className="h-10 bg-background rounded" />
            <div className="h-10 bg-background rounded" />
          </div>
        </div>
      </div>
    }>
      <BacktestInner />
    </Suspense>
  )
}


// ---------------------------------------------------------------------------
// Research Pipeline Strip — workflow map: data → strategy → backtest → record
// ---------------------------------------------------------------------------

const PIPELINE_STEPS: Array<{
  n: number
  label: string
  href: string | null
  caption: string
  current?: boolean
}> = [
  { n: 1, label: 'Pull data', href: '/data', caption: 'fetch_market_data.py · Collection tab' },
  { n: 2, label: 'Write strategy', href: '/workshop', caption: 'scaffold from template' },
  { n: 3, label: 'Backtest', href: null, caption: 'run → tear sheet vs benchmark', current: true },
  { n: 4, label: 'Record', href: '#records', caption: 'promote → IS/OOS validate → 1.5 gate' },
]

function PipelineStrip() {
  return (
    <div className="flex items-center gap-2 flex-wrap px-4 py-2.5 rounded-lg border border-border/50 bg-surface/50">
      {PIPELINE_STEPS.map((step, i) => {
        const inner = (
          <span className="flex items-baseline gap-1.5 whitespace-nowrap">
            <span
              className={`inline-flex items-center justify-center w-4 h-4 rounded-full text-[9px] font-bold self-center ${
                step.current ? 'bg-accent text-white' : 'bg-background text-gray-500 border border-border'
              }`}
            >
              {step.n}
            </span>
            <span className={`text-xs font-medium ${step.current ? 'text-accent' : 'text-gray-300'}`}>
              {step.label}
            </span>
            <span className="text-[10px] text-gray-500 hidden md:inline">{step.caption}</span>
          </span>
        )
        return (
          <span key={step.n} className="flex items-center gap-2">
            {i > 0 && <span className="text-gray-600 text-xs">→</span>}
            {step.href === null ? (
              inner
            ) : step.href.startsWith('#') ? (
              <a href={step.href} className="hover:opacity-80 transition-opacity">
                {inner}
              </a>
            ) : (
              <Link href={step.href} className="hover:opacity-80 transition-opacity">
                {inner}
              </Link>
            )}
          </span>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Promote to Official Record
// ---------------------------------------------------------------------------

function PromotePanel({ result, onPromoted }: { result: BacktestResult; onPromoted: () => void }) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState(`${result.strategy} ${result.symbol}`)
  const [notes, setNotes] = useState('')
  const [busy, setBusy] = useState(false)
  const [promoted, setPromoted] = useState<{ id: string; file: string } | null>(null)
  const [error, setError] = useState('')

  const handlePromote = async () => {
    setBusy(true)
    setError('')
    try {
      const res = await fetch(`${API}/api/backtest/results/${result.backtest_id}/promote`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, notes }),
      })
      if (res.status === 409) {
        setError('Cannot promote: backtest is not completed')
      } else if (!res.ok) {
        const errText = await res.text().catch(() => `Status ${res.status}`)
        setError(`Promote failed: ${errText}`)
      } else {
        const data = await res.json()
        setPromoted({ id: data.id, file: data.file })
        setOpen(false)
        onPromoted()
      }
    } catch (e: any) {
      setError(`Promote failed: ${e.message}`)
    }
    setBusy(false)
  }

  if (promoted) {
    return (
      <div className="rounded-lg border border-warning/30 bg-warning/5 px-4 py-3 space-y-1">
        <p className="text-sm text-warning font-medium">
          Promoted as <span className="font-mono">{promoted.id}</span>
        </p>
        <p className="text-[11px] text-gray-400 font-mono">{promoted.file}</p>
        <p className="text-[11px] text-gray-500">
          candidate — needs IS/OOS validation before the 1.5 gate
        </p>
      </div>
    )
  }

  return (
    <div>
      {!open ? (
        <div className="flex items-center gap-3">
          <button
            onClick={() => setOpen(true)}
            className="px-3 py-1.5 rounded-lg text-xs font-medium border border-warning/40 text-warning hover:bg-warning/10 transition-colors"
          >
            Promote → Official Record
          </button>
          {error && <span className="text-[11px] text-danger">{error}</span>}
        </div>
      ) : (
        <div className="flex items-end gap-3 flex-wrap p-3 rounded-lg border border-border bg-background">
          <div>
            <label className="block text-[11px] text-gray-500 mb-1">Record name</label>
            <input
              type="text"
              value={name}
              onChange={e => setName(e.target.value)}
              className="w-56 bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent"
            />
          </div>
          <div>
            <label className="block text-[11px] text-gray-500 mb-1">Notes (optional)</label>
            <input
              type="text"
              value={notes}
              onChange={e => setNotes(e.target.value)}
              placeholder="e.g. params, data caveats"
              className="w-64 bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent"
            />
          </div>
          <button
            onClick={handlePromote}
            disabled={busy || !name.trim()}
            className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
              busy || !name.trim()
                ? 'bg-warning/30 text-white/50 cursor-not-allowed'
                : 'bg-warning text-black hover:bg-warning/80'
            }`}
          >
            {busy ? 'Promoting...' : 'Promote'}
          </button>
          <button
            onClick={() => {
              setOpen(false)
              setError('')
            }}
            className="px-2 py-1.5 text-xs text-gray-500 hover:text-gray-300 transition-colors"
          >
            Cancel
          </button>
          {error && <span className="text-[11px] text-danger w-full">{error}</span>}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Official Research Records
// ---------------------------------------------------------------------------

interface ResearchRecord {
  id: string
  name?: string
  status: string
  verdict: string
  created?: string
  period?: { start?: string; end?: string; oos_start?: string }
  headline?: Record<string, number | null>
  data_note?: string
  script?: string
  has_curve?: boolean
  rerun?: any
}

interface RerunState {
  status: 'idle' | 'running' | 'completed' | 'failed'
  tail?: string[]
  started_at?: string
  finished_at?: string
  returncode?: number
}

type CurveSeries = Record<string, Array<{ date: string; equity: number }>>

const HEADLINE_DEFS: Array<{ key: string; label: string; fmt: (v: number) => string }> = [
  { key: 'sharpe', label: 'Sharpe', fmt: v => v.toFixed(2) },
  { key: 'oos_sharpe', label: 'OOS Sharpe', fmt: v => v.toFixed(2) },
  { key: 'walkforward_sharpe', label: 'WF Sharpe', fmt: v => v.toFixed(2) },
  { key: 'max_dd', label: 'Max DD', fmt: v => `${(Math.abs(v) <= 1 ? v * 100 : v).toFixed(1)}%` },
  { key: 'cagr', label: 'CAGR', fmt: v => `${(Math.abs(v) <= 1 ? v * 100 : v).toFixed(1)}%` },
  { key: 'correlation', label: 'Corr', fmt: v => v.toFixed(2) },
]

function RerunPanel({
  rec,
  rerun,
  tailOpen,
  onRerun,
  onToggleTail,
}: {
  rec: ResearchRecord
  rerun: RerunState | undefined
  tailOpen: boolean
  onRerun: () => void
  onToggleTail: () => void
}) {
  const isRunning = rerun?.status === 'running'
  const hasTail = (rerun?.tail?.length ?? 0) > 0

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={e => {
            e.stopPropagation()
            onRerun()
          }}
          disabled={isRunning}
          className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors border ${
            isRunning
              ? 'border-border text-gray-500 cursor-not-allowed'
              : 'border-accent/40 text-accent hover:bg-accent/10'
          }`}
        >
          {isRunning ? (
            <span className="flex items-center gap-2">
              <svg className="animate-spin h-3 w-3" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
              </svg>
              Re-running...
            </span>
          ) : (
            'Re-run (reproduce)'
          )}
        </button>
        {rerun && rerun.status !== 'idle' && rerun.status !== 'running' && (
          <span
            className={`text-[11px] font-medium ${
              rerun.status === 'completed' ? 'text-success' : 'text-danger'
            }`}
          >
            {rerun.status === 'completed'
              ? `Completed${rerun.returncode != null ? ` (exit ${rerun.returncode})` : ''}`
              : `Failed${rerun.returncode != null ? ` (exit ${rerun.returncode})` : ''}`}
          </span>
        )}
        {hasTail && (
          <button
            onClick={e => {
              e.stopPropagation()
              onToggleTail()
            }}
            className="text-[11px] text-gray-400 hover:text-gray-200 transition-colors"
          >
            {tailOpen ? '▾ Hide output' : '▸ Show output'}
          </button>
        )}
      </div>
      {hasTail && tailOpen && (
        <pre className="text-[11px] text-gray-400 bg-background border border-border rounded-lg p-3 overflow-x-auto max-h-64 overflow-y-auto font-mono whitespace-pre">
          {rerun!.tail!.join('\n')}
        </pre>
      )}
    </div>
  )
}

function OfficialRecords({ refreshKey = 0 }: { refreshKey?: number }) {
  const [gateRule, setGateRule] = useState('')
  const [records, setRecords] = useState<ResearchRecord[]>([])
  const [loaded, setLoaded] = useState(false)
  const [expandedRec, setExpandedRec] = useState<string | null>(null)
  const [curves, setCurves] = useState<Record<string, CurveSeries>>({})
  const [curveLoading, setCurveLoading] = useState<string | null>(null)
  const [reruns, setReruns] = useState<Record<string, RerunState>>({})
  const [tailOpen, setTailOpen] = useState<Record<string, boolean>>({})
  const pollTimers = useRef<Record<string, ReturnType<typeof setInterval>>>({})

  useEffect(() => {
    const timers = pollTimers.current
    ;(async () => {
      try {
        const res = await fetch(`${API}/api/backtest/records`)
        if (res.ok) {
          const data = await res.json()
          setGateRule(data.gate_rule || '')
          setRecords(data.records || [])
        }
      } catch {}
      setLoaded(true)
    })()
    return () => {
      Object.values(timers).forEach(t => clearInterval(t))
      Object.keys(timers).forEach(k => delete timers[k])
    }
  }, [refreshKey])

  const pollRerun = useCallback((id: string) => {
    if (pollTimers.current[id]) return
    const tick = async () => {
      try {
        const res = await fetch(`${API}/api/backtest/records/${id}/rerun/status`)
        if (!res.ok) return
        const data: RerunState = await res.json()
        setReruns(prev => ({ ...prev, [id]: data }))
        if (data.status !== 'running') {
          if (pollTimers.current[id]) {
            clearInterval(pollTimers.current[id])
            delete pollTimers.current[id]
          }
          if ((data.tail?.length ?? 0) > 0) {
            setTailOpen(prev => ({ ...prev, [id]: true }))
          }
        }
      } catch {}
    }
    tick()
    pollTimers.current[id] = setInterval(tick, 3000)
  }, [])

  const handleRerun = useCallback(
    async (id: string) => {
      setReruns(prev => ({ ...prev, [id]: { ...prev[id], status: 'running' } }))
      try {
        const res = await fetch(`${API}/api/backtest/records/${id}/rerun`, { method: 'POST' })
        if (res.ok || res.status === 409) {
          // 409 = already running — just start polling
          pollRerun(id)
        } else {
          const errText = await res.text().catch(() => `Status ${res.status}`)
          setReruns(prev => ({ ...prev, [id]: { status: 'failed', tail: [errText] } }))
          setTailOpen(prev => ({ ...prev, [id]: true }))
        }
      } catch (e: any) {
        setReruns(prev => ({ ...prev, [id]: { status: 'failed', tail: [`Error: ${e.message}`] } }))
        setTailOpen(prev => ({ ...prev, [id]: true }))
      }
    },
    [pollRerun]
  )

  const toggleCurve = useCallback(
    async (rec: ResearchRecord) => {
      if (expandedRec === rec.id) {
        setExpandedRec(null)
        return
      }
      setExpandedRec(rec.id)
      if (rec.has_curve && !curves[rec.id]) {
        setCurveLoading(rec.id)
        try {
          const res = await fetch(`${API}/api/backtest/records/${rec.id}/curve`)
          if (res.ok) {
            const data = await res.json()
            setCurves(prev => ({ ...prev, [rec.id]: data.series || {} }))
          }
        } catch {}
        setCurveLoading(null)
      }
    },
    [expandedRec, curves]
  )

  if (loaded && records.length === 0) return null

  const validated = records.filter(r => r.status !== 'rejected')
  const rejected = records.filter(r => r.status === 'rejected')
  const nCandidates = validated.filter(r => r.status === 'candidate').length

  const renderHeadline = (rec: ResearchRecord) =>
    HEADLINE_DEFS.filter(d => rec.headline && typeof rec.headline[d.key] === 'number').map(d => (
      <div key={d.key}>
        <p className="text-[10px] text-gray-500">{d.label}</p>
        <p
          className={`text-sm font-bold ${
            d.key === 'max_dd' ? 'text-danger' : d.key === 'oos_sharpe' ? 'text-accent' : 'text-gray-100'
          }`}
        >
          {d.fmt(rec.headline![d.key] as number)}
        </p>
      </div>
    ))

  const renderCurveBlock = (rec: ResearchRecord) => {
    if (expandedRec !== rec.id) return null
    return (
      <div className="mt-3 pt-3 border-t border-border/50 space-y-4" onClick={e => e.stopPropagation()}>
        {rec.has_curve && (
          curveLoading === rec.id ? (
            <div className="h-56 bg-background rounded-lg animate-pulse" />
          ) : curves[rec.id] ? (
            <MultiSeriesCurveSVG series={curves[rec.id]} />
          ) : (
            <p className="text-xs text-gray-500 py-4 text-center">Failed to load curve</p>
          )
        )}
        <TearSheet analyticsUrl={`/api/backtest/records/${rec.id}/analytics`} />
      </div>
    )
  }

  return (
    <div id="records" className="bg-surface rounded-xl border border-border p-6 space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-lg font-semibold">Official Research Records</h3>
          {gateRule && (
            <p className="text-xs text-gray-500 mt-0.5">
              Gate rule: <span className="text-gray-400">{gateRule}</span>
            </p>
          )}
        </div>
        <span className="text-xs text-gray-500">
          {loaded
            ? `${validated.length - nCandidates} validated${nCandidates > 0 ? ` · ${nCandidates} candidate` : ''} · ${rejected.length} rejected`
            : 'Loading...'}
        </span>
      </div>

      {!loaded && <div className="h-32 bg-background rounded-lg animate-pulse" />}

      {/* Validated + candidate records — prominent cards */}
      {validated.map(rec => {
        const isCandidate = rec.status === 'candidate'
        return (
        <div
          key={rec.id}
          className={`rounded-xl border p-5 transition-colors cursor-pointer ${
            isCandidate
              ? 'border-warning/30 bg-warning/5 hover:border-warning/60'
              : 'border-success/30 bg-success/5 hover:border-success/60'
          }`}
          onClick={() => toggleCurve(rec)}
        >
          <div className="flex items-start justify-between gap-4 flex-wrap">
            <div className="min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <h4 className="font-semibold text-gray-100">{rec.name || rec.id}</h4>
                <span className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${
                  isCandidate ? 'bg-warning/15 text-warning' : 'bg-success/15 text-success'
                }`}>
                  {rec.status}
                </span>
                <span className="text-[10px] text-gray-500">
                  {expandedRec === rec.id ? '▾ hide tear sheet' : '▸ click for tear sheet'}
                </span>
              </div>
              <p className={`text-xs mt-1 ${isCandidate ? 'text-warning/90' : 'text-success/90'}`}>{rec.verdict}</p>
              {rec.period && (
                <p className="text-[11px] text-gray-500 mt-1">
                  {rec.period.start} → {rec.period.end}
                  {rec.period.oos_start && <span className="text-gray-400"> · OOS from {rec.period.oos_start}</span>}
                  {rec.created && <span> · recorded {rec.created}</span>}
                </p>
              )}
            </div>
            <div className="flex items-center gap-4 flex-shrink-0">{renderHeadline(rec)}</div>
          </div>

          {(rec.script || rec.data_note) && (
            <div className="mt-3">
              {rec.script && <p className="text-[10px] text-gray-500 font-mono">{rec.script}</p>}
              {rec.data_note && <p className="text-[11px] text-gray-500 mt-0.5">{rec.data_note}</p>}
            </div>
          )}

          <div className="mt-3" onClick={e => e.stopPropagation()}>
            <RerunPanel
              rec={rec}
              rerun={reruns[rec.id]}
              tailOpen={!!tailOpen[rec.id]}
              onRerun={() => handleRerun(rec.id)}
              onToggleTail={() => setTailOpen(prev => ({ ...prev, [rec.id]: !prev[rec.id] }))}
            />
          </div>

          {renderCurveBlock(rec)}
        </div>
        )
      })}

      {/* Rejected records — compact rows */}
      {rejected.length > 0 && (
        <div className="space-y-2">
          <p className="text-[11px] font-medium uppercase tracking-wider text-gray-500">Rejected</p>
          {rejected.map(rec => (
            <div
              key={rec.id}
              className="rounded-lg border-l-2 border-danger/50 bg-background px-4 py-2.5 cursor-pointer hover:bg-background/70"
              onClick={() => toggleCurve(rec)}
            >
              <div className="flex items-center justify-between gap-3 flex-wrap">
                <div className="flex items-center gap-2 min-w-0 flex-wrap">
                  <span className="text-sm font-medium text-gray-200">{rec.name || rec.id}</span>
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase bg-danger/10 text-danger">
                    rejected
                  </span>
                  {rec.headline?.oos_sharpe != null && (
                    <span className="text-xs text-gray-400">
                      OOS Sharpe <span className="text-danger font-semibold">{(rec.headline.oos_sharpe as number).toFixed(2)}</span>
                    </span>
                  )}
                  <span className="text-[11px] text-gray-500 italic truncate">{rec.verdict}</span>
                </div>
                <div onClick={e => e.stopPropagation()} className="flex-shrink-0">
                  <RerunPanel
                    rec={rec}
                    rerun={reruns[rec.id]}
                    tailOpen={!!tailOpen[rec.id]}
                    onRerun={() => handleRerun(rec.id)}
                    onToggleTail={() => setTailOpen(prev => ({ ...prev, [rec.id]: !prev[rec.id] }))}
                  />
                </div>
              </div>
              {rec.script && <p className="text-[10px] text-gray-600 font-mono mt-1">{rec.script}</p>}
              {renderCurveBlock(rec)}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Multi-series SVG Equity Curve (records — rebased to 100k)
// ---------------------------------------------------------------------------

const SERIES_COLORS: Record<string, string> = {
  wheel: '#3b82f6',
  sniper: '#f59e0b',
  combo_70_30: '#10b981',
}
const FALLBACK_COLORS = ['#8b5cf6', '#ec4899', '#14b8a6', '#eab308']

function MultiSeriesCurveSVG({ series }: { series: CurveSeries }) {
  const names = Object.keys(series).filter(n => (series[n]?.length ?? 0) > 1)
  if (names.length === 0) {
    return <p className="text-xs text-gray-500 py-4 text-center">No curve data</p>
  }

  const BASELINE = 100000
  let minVal = BASELINE
  let maxVal = BASELINE
  for (const n of names) {
    for (const p of series[n]) {
      if (p.equity < minVal) minVal = p.equity
      if (p.equity > maxVal) maxVal = p.equity
    }
  }
  minVal *= 0.995
  maxVal *= 1.005
  const range = maxVal - minVal || 1

  const w = 800
  const h = 240
  const chartH = h - 20 // reserve space for date labels

  const yOf = (v: number) => chartH - ((v - minVal) / range) * chartH

  const paths = names.map((n, idx) => {
    const pts = series[n]
    const d = pts
      .map((p, i) => `${i === 0 ? 'M' : 'L'}${((i / (pts.length - 1)) * w).toFixed(2)},${yOf(p.equity).toFixed(2)}`)
      .join(' ')
    const color = SERIES_COLORS[n] ?? FALLBACK_COLORS[idx % FALLBACK_COLORS.length]
    const isCombo = n.startsWith('combo')
    return { name: n, d, color, width: isCombo ? 2.5 : 1.5 }
  })

  // Date axis labels from the longest series: first / mid / last
  const longest = names.reduce((a, b) => (series[a].length >= series[b].length ? a : b))
  const dates = series[longest]
  const firstDate = dates[0]?.date ?? ''
  const midDate = dates[Math.floor(dates.length / 2)]?.date ?? ''
  const lastDate = dates[dates.length - 1]?.date ?? ''
  const baselineY = yOf(BASELINE)

  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full">
        {/* 100k baseline */}
        <line x1="0" y1={baselineY} x2={w} y2={baselineY} stroke="#374151" strokeWidth="1" strokeDasharray="4" />
        <text x="4" y={baselineY - 4} fill="#6b7280" fontSize="10">
          $100k
        </text>
        {paths.map(p => (
          <path key={p.name} d={p.d} fill="none" stroke={p.color} strokeWidth={p.width} />
        ))}
        {/* Date axis */}
        <text x="0" y={h - 4} fill="#6b7280" fontSize="10">
          {firstDate}
        </text>
        <text x={w / 2} y={h - 4} fill="#6b7280" fontSize="10" textAnchor="middle">
          {midDate}
        </text>
        <text x={w} y={h - 4} fill="#6b7280" fontSize="10" textAnchor="end">
          {lastDate}
        </text>
      </svg>
      {/* Legend */}
      <div className="flex items-center gap-4 mt-2 flex-wrap">
        {paths.map(p => (
          <span key={p.name} className="flex items-center gap-1.5 text-[11px] text-gray-400">
            <span className="inline-block w-4 h-0.5 rounded" style={{ backgroundColor: p.color }} />
            {p.name}
          </span>
        ))}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// SVG Equity Curve Component
// ---------------------------------------------------------------------------

function EquityCurveSVG({ data, initialCash }: { data: Array<Record<string, any>>; initialCash: number }) {
  if (data.length < 2) return null

  const values = data.map(d => d.total_value ?? d.equity ?? 0)
  const minVal = Math.min(...values) * 0.999
  const maxVal = Math.max(...values) * 1.001
  const range = maxVal - minVal || 1

  const w = 800
  const h = 160
  const points = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w
    const y = h - ((v - minVal) / range) * h
    return `${x},${y}`
  })

  const pathD = `M${points.join(' L')}`
  const areaD = `${pathD} L${w},${h} L0,${h} Z`

  const finalVal = values[values.length - 1]
  const isProfit = finalVal >= initialCash

  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-full" preserveAspectRatio="none">
      <defs>
        <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={isProfit ? '#10b981' : '#ef4444'} stopOpacity="0.3" />
          <stop offset="100%" stopColor={isProfit ? '#10b981' : '#ef4444'} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <line
        x1="0"
        y1={h - ((initialCash - minVal) / range) * h}
        x2={w}
        y2={h - ((initialCash - minVal) / range) * h}
        stroke="#374151"
        strokeWidth="1"
        strokeDasharray="4"
      />
      <path d={areaD} fill="url(#eqGrad)" />
      <path d={pathD} fill="none" stroke={isProfit ? '#10b981' : '#ef4444'} strokeWidth="2" />
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Runtime v2 Workbench — button-driven research loop over /api/backtest/v2
// ---------------------------------------------------------------------------

interface V2ParamSpec {
  name: string
  type: string
  default: any
  min: number | null
  max: number | null
  step: number | null
  choices: any[] | null
  help?: string | null
}

interface V2Strategy {
  id: string
  book_type: string
  asset_domain: string
  universe: string[]
  primary_tf: string
  cadence_sec: number
  maker_only: boolean
  params_schema: V2ParamSpec[]
}

interface V2CatalogEntry {
  asset_class: string
  symbol: string
  freq: string
  rows: number
  start?: string
  end?: string
}

interface V2RunEntry {
  id: string
  strategyId: string
  params: Record<string, any>
  paramsDiff: string
  status: 'running' | 'completed' | 'failed'
  sharpe: number | null
  error?: string
}

interface V2SweepRow {
  params: Record<string, any>
  sharpe_ratio?: number
  max_drawdown?: number
  total_return_pct?: number
  total_trades?: number
  win_rate?: number
  profit_factor?: number
  error?: string
}

interface V2SweepState {
  status: string
  total: number
  done: number
  results: V2SweepRow[]
}

interface V2CoverageSpan {
  start: string
  end: string
  rows?: number
}

interface V2Coverage {
  asset_class: string
  freq: string
  symbols: Record<string, V2CoverageSpan | null>
  intersection: { start: string; end: string } | null
}

interface V2MissingItem {
  symbol: string
  have: [string, string] | null
  requested: [string, string]
  reason: 'does_not_exist_pre_listing' | 'not_collected' | 'not_in_catalog' | string
  collectable: boolean
  hint?: string
}

interface V2InsufficientPayload {
  error: string
  missing: V2MissingItem[]
  coverage?: V2Coverage
}

interface V2WfFold {
  train: [string, string]
  test: [string, string]
  best_params?: Record<string, any>
  train_sharpe?: number | null
  test_sharpe?: number | null
  test_return_pct?: number | null
  test_max_dd?: number | null
  test_trades?: number | null
}

interface V2WfState {
  status: 'running' | 'completed' | 'failed' | string
  folds_done: number
  folds_total: number
  folds: V2WfFold[]
  stitched_curve?: Array<{ timestamp: string; total_value: number }>
  oos_sharpe?: number | null
  oos_return_pct?: number | null
  gate?: { threshold: number; passed: boolean }
  error?: string
  detail?: { have_days?: number; need_days_min?: number; hint?: string }
}

const V2_ASSET_CLASS = 'tokenized_stock'
const V2_DAY_MS = 86400000
const V2_RANGE_PRESETS = ['1W', '1M', '3M', 'All', 'Custom'] as const
type V2RangePreset = (typeof V2_RANGE_PRESETS)[number]
type V2Mode = 'run' | 'sweep' | 'wf'
type V2ResultTab = 'overview' | 'tearsheet' | 'trades'

function v2DateMs(d: string): number {
  return Date.parse(d)
}

function v2MsToISO(ms: number): string {
  return new Date(ms).toISOString().slice(0, 10)
}
const V2_COLORS = ['#10b981', '#3b82f6', '#f59e0b', '#ec4899']

function v2Defaults(s: V2Strategy): Record<string, any> {
  const out: Record<string, any> = {}
  for (const p of s.params_schema) out[p.name] = p.default
  return out
}

function v2ParamsDiff(params: Record<string, any>, defaults: Record<string, any>): string {
  const parts = Object.entries(params)
    .filter(([k, v]) => defaults[k] !== v)
    .map(([k, v]) => `${k}=${v}`)
  return parts.length > 0 ? parts.join(' ') : 'defaults'
}

function v2AsPct(v: number | null | undefined): number {
  if (v == null || isNaN(v)) return 0
  return Math.abs(v) <= 1.5 ? v * 100 : v
}

function v2EquityOf(p: Record<string, any>): number {
  return p.total_value ?? p.equity ?? p.value ?? 0
}

function RuntimeV2Workbench() {
  const [strategies, setStrategies] = useState<V2Strategy[]>([])
  const [catalog, setCatalog] = useState<V2CatalogEntry[]>([])
  const [loaded, setLoaded] = useState(false)
  const [loadError, setLoadError] = useState('')

  const [selId, setSelId] = useState('')
  const [params, setParams] = useState<Record<string, any>>({})
  const [symbols, setSymbols] = useState<string[]>([])
  const [freq, setFreq] = useState('5m')
  const [equity, setEquity] = useState('5000')

  const [running, setRunning] = useState(false)
  const [runMsg, setRunMsg] = useState('')
  const [activeResult, setActiveResult] = useState<any | null>(null)
  const [showTrades, setShowTrades] = useState(false)

  const [history, setHistory] = useState<V2RunEntry[]>([])
  const [compareIds, setCompareIds] = useState<string[]>([])
  const [resultCache, setResultCache] = useState<Record<string, any>>({})

  const [mode, setMode] = useState<V2Mode>('run')

  const [sweepP1, setSweepP1] = useState('')
  const [sweepP2, setSweepP2] = useState('')
  const [sweepV1, setSweepV1] = useState('')
  const [sweepV2, setSweepV2] = useState('')
  const [sweepBusy, setSweepBusy] = useState(false)
  const [sweepMsg, setSweepMsg] = useState('')
  const [sweepState, setSweepState] = useState<V2SweepState | null>(null)
  const [sweepView, setSweepView] = useState<'heatmap' | 'table'>('heatmap')

  // Single-run result tabs
  const [resultTab, setResultTab] = useState<V2ResultTab>('overview')

  // Date range + coverage
  const [coverage, setCoverage] = useState<V2Coverage | null>(null)
  const [covLoading, setCovLoading] = useState(false)
  const [rangePreset, setRangePreset] = useState<V2RangePreset>('All')
  const [startDate, setStartDate] = useState('')
  const [endDate, setEndDate] = useState('')

  // Insufficient-data (HTTP 422) dialog
  const [insufficient, setInsufficient] = useState<V2InsufficientPayload | null>(null)

  // Walk-forward
  const [wfTrain, setWfTrain] = useState('2')
  const [wfTest, setWfTest] = useState('1')
  const [wfStep, setWfStep] = useState('1')
  const [wfOptimize, setWfOptimize] = useState(false)
  const [wfP1, setWfP1] = useState('')
  const [wfP2, setWfP2] = useState('')
  const [wfV1, setWfV1] = useState('')
  const [wfV2, setWfV2] = useState('')
  const [wfBusy, setWfBusy] = useState(false)
  const [wfMsg, setWfMsg] = useState('')
  const [wfState, setWfState] = useState<V2WfState | null>(null)

  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => {
      aliveRef.current = false
    }
  }, [])

  const sel = strategies.find(s => s.id === selId)
  const defaults = sel ? v2Defaults(sel) : {}

  // Load strategies + catalog on mount
  useEffect(() => {
    ;(async () => {
      try {
        const [sRes, cRes] = await Promise.all([
          fetch(`${API}/api/backtest/v2/strategies`),
          fetch(`${API}/api/data/catalog`),
        ])
        if (sRes.ok) {
          const sData = await sRes.json()
          const strats: V2Strategy[] = sData.strategies || []
          setStrategies(strats)
          if (strats.length > 0) {
            const first = strats[0]
            setSelId(first.id)
            setParams(v2Defaults(first))
            setSymbols(first.universe)
            setFreq(first.primary_tf)
          }
        } else {
          setLoadError(`v2 strategies endpoint returned ${sRes.status}`)
        }
        if (cRes.ok) {
          const cData = await cRes.json()
          setCatalog(cData.entries || [])
        }
      } catch (e: any) {
        setLoadError(`Failed to reach backend: ${e.message}`)
      }
      setLoaded(true)
    })()
  }, [])

  const pickStrategy = (s: V2Strategy) => {
    setSelId(s.id)
    setParams(v2Defaults(s))
    setSymbols(s.universe)
    setFreq(s.primary_tf)
    setSweepP1('')
    setSweepP2('')
    setSweepV1('')
    setSweepV2('')
    setSweepState(null)
    setSweepMsg('')
    setWfP1('')
    setWfP2('')
    setWfV1('')
    setWfV2('')
    setWfState(null)
    setWfMsg('')
    setInsufficient(null)
  }

  // -------------------------------------------------------------------------
  // Coverage + date range
  // -------------------------------------------------------------------------

  const applyPreset = (p: V2RangePreset, cov: V2Coverage | null = coverage) => {
    setRangePreset(p)
    if (p === 'Custom') return
    const inter = cov?.intersection
    if (!inter) return
    if (p === 'All') {
      setStartDate(inter.start)
      setEndDate(inter.end)
      return
    }
    const days = p === '1W' ? 7 : p === '1M' ? 30 : 90
    const endMs = v2DateMs(inter.end)
    const startMs = Math.max(v2DateMs(inter.start), endMs - days * V2_DAY_MS)
    setStartDate(v2MsToISO(startMs))
    setEndDate(inter.end)
  }

  const presetRef = useRef<V2RangePreset>('All')
  useEffect(() => {
    presetRef.current = rangePreset
  }, [rangePreset])

  const symbolsKey = symbols.join(',')
  useEffect(() => {
    if (!selId || symbols.length === 0) {
      setCoverage(null)
      return
    }
    let cancelled = false
    ;(async () => {
      setCovLoading(true)
      try {
        const qs = new URLSearchParams({
          symbols: symbolsKey,
          freq,
          asset_class: V2_ASSET_CLASS,
        })
        const res = await fetch(`${API}/api/backtest/v2/coverage?${qs.toString()}`)
        if (res.ok) {
          const data: V2Coverage = await res.json()
          if (cancelled) return
          setCoverage(data)
          // Re-derive the window from the (possibly changed) coverage
          const prev = presetRef.current
          const inter = data.intersection
          if (prev !== 'Custom' && inter) {
            if (prev === 'All') {
              setStartDate(inter.start)
              setEndDate(inter.end)
            } else {
              const days = prev === '1W' ? 7 : prev === '1M' ? 30 : 90
              const endMs = v2DateMs(inter.end)
              const startMs = Math.max(v2DateMs(inter.start), endMs - days * V2_DAY_MS)
              setStartDate(v2MsToISO(startMs))
              setEndDate(inter.end)
            }
          }
        } else if (!cancelled) {
          setCoverage(null)
        }
      } catch {
        if (!cancelled) setCoverage(null)
      }
      if (!cancelled) setCovLoading(false)
    })()
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selId, symbolsKey, freq])

  // Axis bounds across all covered symbols (min start → max end)
  const covAxis = (() => {
    if (!coverage) return null
    const spans = Object.values(coverage.symbols).filter((s): s is V2CoverageSpan => s != null)
    if (spans.length === 0) return null
    const start = Math.min(...spans.map(s => v2DateMs(s.start)))
    const end = Math.max(...spans.map(s => v2DateMs(s.end)))
    if (!isFinite(start) || !isFinite(end) || end <= start) return null
    return { start, end }
  })()

  const truncateToAvailable = () => {
    const inter = insufficient?.coverage?.intersection ?? coverage?.intersection
    if (inter) {
      setStartDate(inter.start)
      setEndDate(inter.end)
      setRangePreset('Custom')
    }
    setInsufficient(null)
  }

  // Parse a failed response; returns a display message, or null if the
  // insufficient-data dialog took over.
  const handleFailedResponse = async (res: Response): Promise<string | null> => {
    const text = await res.text().catch(() => '')
    if (res.status === 422) {
      try {
        const j = JSON.parse(text)
        const payload: V2InsufficientPayload = j?.detail?.error ? j.detail : j
        if (payload?.error === 'insufficient_data') {
          setInsufficient(payload)
          return null
        }
      } catch {}
    }
    return text || `Status ${res.status}`
  }

  // Symbol choices: catalog tokenized symbols ∪ strategy universe
  const symbolChoices = (() => {
    const set = new Set<string>(sel?.universe || [])
    for (const e of catalog) if (e.asset_class === V2_ASSET_CLASS) set.add(e.symbol)
    return Array.from(set).sort()
  })()

  // Freq options: catalog freqs for selected symbols ∪ primary_tf
  const freqChoices = (() => {
    const set = new Set<string>()
    if (sel) set.add(sel.primary_tf)
    for (const e of catalog) {
      if (e.asset_class === V2_ASSET_CLASS && symbols.includes(e.symbol)) set.add(e.freq)
    }
    return Array.from(set).sort()
  })()

  const toggleSymbol = (sym: string) => {
    setSymbols(prev => (prev.includes(sym) ? prev.filter(s => s !== sym) : [...prev, sym]))
  }

  const setParam = (name: string, value: any) => {
    setParams(prev => ({ ...prev, [name]: value }))
  }

  // -------------------------------------------------------------------------
  // Run
  // -------------------------------------------------------------------------

  const runV2 = async () => {
    if (!sel) return
    setRunning(true)
    setActiveResult(null)
    setShowTrades(false)
    setRunMsg('Submitting v2 run...')
    try {
      const body = {
        strategy_id: sel.id,
        params,
        symbols,
        asset_class: V2_ASSET_CLASS,
        freq,
        initial_equity: parseFloat(equity) || 5000,
        start: startDate || undefined,
        end: endDate || undefined,
      }
      const res = await fetch(`${API}/api/backtest/v2/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const errText = await handleFailedResponse(res)
        setRunMsg(errText ? `Error: ${errText}` : 'Insufficient data for the selected window')
        setRunning(false)
        return
      }
      const data = await res.json()
      const id: string = data.backtest_id
      const entry: V2RunEntry = {
        id,
        strategyId: sel.id,
        params: { ...params },
        paramsDiff: v2ParamsDiff(params, v2Defaults(sel)),
        status: 'running',
        sharpe: null,
      }
      setHistory(prev => [entry, ...prev])
      setRunMsg(`Running ${id}...`)

      // Poll every 2s
      for (let i = 0; i < 300; i++) {
        await new Promise(r => setTimeout(r, 2000))
        if (!aliveRef.current) return
        try {
          const st = await fetch(`${API}/api/backtest/status/${id}`)
          if (!st.ok) continue
          const stData = await st.json()
          setRunMsg(`Status: ${stData.status} (${(i + 1) * 2}s)`)
          if (stData.status === 'completed') {
            const fr = await fetch(`${API}/api/backtest/results/${id}`)
            if (fr.ok) {
              const full = await fr.json()
              if (!aliveRef.current) return
              setActiveResult(full)
              setResultCache(prev => ({ ...prev, [id]: full }))
              const sharpe = full?.metrics?.sharpe_ratio ?? null
              setHistory(prev =>
                prev.map(h => (h.id === id ? { ...h, status: 'completed', sharpe } : h))
              )
              setRunMsg('Completed')
            } else {
              setRunMsg(`Completed but result fetch failed (${fr.status})`)
              setHistory(prev => prev.map(h => (h.id === id ? { ...h, status: 'failed', error: `result fetch ${fr.status}` } : h)))
            }
            setRunning(false)
            return
          }
          if (stData.status === 'failed') {
            const err = stData.error || 'unknown error'
            setActiveResult({ backtest_id: id, status: 'failed', error: err })
            setHistory(prev => prev.map(h => (h.id === id ? { ...h, status: 'failed', error: err } : h)))
            setRunMsg(`Failed: ${err}`)
            setRunning(false)
            return
          }
        } catch {}
      }
      setRunMsg('Timeout waiting for v2 run')
      setRunning(false)
    } catch (e: any) {
      setRunMsg(`Error: ${e.message}`)
      setRunning(false)
    }
  }

  // -------------------------------------------------------------------------
  // Compare
  // -------------------------------------------------------------------------

  const toggleCompare = async (id: string) => {
    setCompareIds(prev => {
      if (prev.includes(id)) return prev.filter(x => x !== id)
      if (prev.length >= 4) return prev
      return [...prev, id]
    })
    if (!resultCache[id]) {
      try {
        const res = await fetch(`${API}/api/backtest/results/${id}`)
        if (res.ok) {
          const data = await res.json()
          setResultCache(prev => ({ ...prev, [id]: data }))
        }
      } catch {}
    }
  }

  // -------------------------------------------------------------------------
  // Sweep
  // -------------------------------------------------------------------------

  const autoRange = (spec: V2ParamSpec): string => {
    if (spec.min == null || spec.max == null) return String(spec.default)
    const n = 5
    const vals: number[] = []
    for (let i = 0; i < n; i++) {
      let v = spec.min + ((spec.max - spec.min) * i) / (n - 1)
      if (spec.type === 'int') v = Math.round(v)
      else v = Math.round(v * 10000) / 10000
      vals.push(v)
    }
    return Array.from(new Set(vals)).join(', ')
  }

  const parseValues = (spec: V2ParamSpec, text: string): number[] => {
    return text
      .split(',')
      .map(t => t.trim())
      .filter(t => t.length > 0)
      .map(t => (spec.type === 'int' ? parseInt(t, 10) : parseFloat(t)))
      .filter(v => !isNaN(v))
  }

  const runSweep = async () => {
    if (!sel || !sweepP1) return
    const spec1 = sel.params_schema.find(p => p.name === sweepP1)
    if (!spec1) return
    const vals1 = parseValues(spec1, sweepV1)
    if (vals1.length === 0) {
      setSweepMsg('Enter at least one value for the first parameter')
      return
    }
    const sweep: Record<string, number[]> = { [sweepP1]: vals1 }
    if (sweepP2 && sweepP2 !== sweepP1) {
      const spec2 = sel.params_schema.find(p => p.name === sweepP2)
      if (spec2) {
        const vals2 = parseValues(spec2, sweepV2)
        if (vals2.length > 0) sweep[sweepP2] = vals2
      }
    }
    setSweepBusy(true)
    setSweepState(null)
    setSweepMsg('Submitting sweep...')
    try {
      const body = {
        strategy_id: sel.id,
        base_params: params,
        sweep,
        symbols,
        asset_class: V2_ASSET_CLASS,
        freq,
        initial_equity: parseFloat(equity) || 5000,
        start: startDate || undefined,
        end: endDate || undefined,
      }
      const res = await fetch(`${API}/api/backtest/v2/sweep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const errText = await handleFailedResponse(res)
        setSweepMsg(errText ? `Error: ${errText}` : 'Insufficient data for the selected window')
        setSweepBusy(false)
        return
      }
      const data = await res.json()
      const sweepId: string = data.sweep_id
      setSweepMsg(`Sweep ${sweepId}: ${data.combos} combos queued`)

      for (let i = 0; i < 600; i++) {
        await new Promise(r => setTimeout(r, 2000))
        if (!aliveRef.current) return
        try {
          const st = await fetch(`${API}/api/backtest/v2/sweep/${sweepId}`)
          if (!st.ok) continue
          const stData: V2SweepState = await st.json()
          setSweepState(stData)
          setSweepMsg(`Sweep ${stData.status}: ${stData.done}/${stData.total}`)
          if (stData.status === 'completed' || stData.status === 'failed') {
            setSweepBusy(false)
            return
          }
        } catch {}
      }
      setSweepMsg('Timeout waiting for sweep')
      setSweepBusy(false)
    } catch (e: any) {
      setSweepMsg(`Error: ${e.message}`)
      setSweepBusy(false)
    }
  }

  const applySweepRow = (row: V2SweepRow) => {
    if (row.error) return
    setParams(prev => ({ ...prev, ...row.params }))
    setSweepMsg(`Applied ${v2ParamsDiff(row.params, {})} to form`)
  }

  // -------------------------------------------------------------------------
  // Walk-forward
  // -------------------------------------------------------------------------

  const runWalkforward = async () => {
    if (!sel) return
    const trainD = parseInt(wfTrain, 10)
    const testD = parseInt(wfTest, 10)
    const stepD = parseInt(wfStep, 10)
    if (!trainD || !testD || trainD < 1 || testD < 1) {
      setWfMsg('train_days and test_days must be >= 1')
      return
    }
    let optimize: Record<string, number[]> | undefined
    if (wfOptimize && wfP1) {
      const spec1 = sel.params_schema.find(p => p.name === wfP1)
      if (spec1) {
        const vals1 = parseValues(spec1, wfV1)
        if (vals1.length === 0) {
          setWfMsg('Enter at least one value for the first optimize parameter')
          return
        }
        optimize = { [wfP1]: vals1 }
        if (wfP2 && wfP2 !== wfP1) {
          const spec2 = sel.params_schema.find(p => p.name === wfP2)
          if (spec2) {
            const vals2 = parseValues(spec2, wfV2)
            if (vals2.length > 0) optimize[wfP2] = vals2
          }
        }
      }
    }
    setWfBusy(true)
    setWfState(null)
    setWfMsg('Submitting walk-forward...')
    try {
      const body = {
        strategy_id: sel.id,
        params,
        symbols,
        asset_class: V2_ASSET_CLASS,
        freq,
        initial_equity: parseFloat(equity) || 5000,
        start: startDate || undefined,
        end: endDate || undefined,
        train_days: trainD,
        test_days: testD,
        step_days: stepD || 1,
        ...(optimize ? { optimize } : {}),
      }
      const res = await fetch(`${API}/api/backtest/v2/walkforward`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      if (!res.ok) {
        const errText = await handleFailedResponse(res)
        setWfMsg(errText ? `Error: ${errText}` : 'Insufficient data for the selected window')
        setWfBusy(false)
        return
      }
      const data = await res.json()
      const wfId: string = data.walkforward_id
      setWfMsg(`Walk-forward ${wfId} started...`)

      for (let i = 0; i < 900; i++) {
        await new Promise(r => setTimeout(r, 2000))
        if (!aliveRef.current) return
        try {
          const st = await fetch(`${API}/api/backtest/v2/walkforward/${wfId}`)
          if (!st.ok) continue
          const stData: V2WfState = await st.json()
          setWfState(stData)
          if (stData.status === 'completed') {
            setWfMsg(`Completed — ${stData.folds_total} folds`)
            setWfBusy(false)
            return
          }
          if (stData.status === 'failed') {
            setWfMsg(`Failed: ${stData.error || 'unknown error'}`)
            setWfBusy(false)
            return
          }
          setWfMsg(`Running fold ${stData.folds_done}/${stData.folds_total}...`)
        } catch {}
      }
      setWfMsg('Timeout waiting for walk-forward')
      setWfBusy(false)
    } catch (e: any) {
      setWfMsg(`Error: ${e.message}`)
      setWfBusy(false)
    }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const m2 = activeResult?.metrics || {}
  const compareRuns = compareIds
    .map(id => ({ id, run: history.find(h => h.id === id), result: resultCache[id] }))
    .filter(x => x.run && x.result && x.result.equity_curve && x.result.equity_curve.length > 1)

  const sortedSweepRows = sweepState
    ? [...sweepState.results].sort((a, b) => {
        if (a.error && !b.error) return 1
        if (!a.error && b.error) return -1
        return (b.sharpe_ratio ?? -Infinity) - (a.sharpe_ratio ?? -Infinity)
      })
    : []

  // Sweep dimensionality — 2-param sweeps unlock the heatmap view
  const sweepKeys = sweepState && sweepState.results.length > 0
    ? Object.keys(sweepState.results[0].params || {})
    : []
  const isTwoParamSweep = sweepKeys.length === 2

  // Shared 1–2 param picker (used by Sweep and Walk-Forward optimize)
  const renderParamSlots = (
    slots: Array<{
      pick: string
      setPick: (v: string) => void
      vals: string
      setVals: (v: string) => void
      label: string
    }>
  ) => {
    if (!sel) return null
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {slots.map((slot, idx) => {
          const spec = sel.params_schema.find(p => p.name === slot.pick)
          return (
            <div key={idx} className="space-y-1.5">
              <label className="block text-[11px] text-gray-500">{slot.label}</label>
              <select
                value={slot.pick}
                onChange={e => {
                  slot.setPick(e.target.value)
                  const sp = sel.params_schema.find(p => p.name === e.target.value)
                  if (sp) slot.setVals(autoRange(sp))
                  else slot.setVals('')
                }}
                className="w-full bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent font-mono"
              >
                <option value="">— none —</option>
                {sel.params_schema
                  .filter(p => p.type === 'float' || p.type === 'int')
                  .map(p => (
                    <option key={p.name} value={p.name}>{p.name}</option>
                  ))}
              </select>
              {slot.pick && (
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={slot.vals}
                    onChange={e => slot.setVals(e.target.value)}
                    placeholder="comma-separated values"
                    className="flex-1 bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent font-mono"
                  />
                  {spec && (
                    <button
                      onClick={() => slot.setVals(autoRange(spec))}
                      className="text-[11px] text-accent hover:underline whitespace-nowrap"
                    >
                      auto 5
                    </button>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>
    )
  }

  return (
    <div className="bg-surface rounded-xl border border-border p-6 space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div>
          <h3 className="text-lg font-semibold">Runtime v2 Workbench</h3>
          <p className="text-xs text-gray-500 mt-0.5">
            Button-driven research loop — schema params, real runs, sweeps, run compare
          </p>
        </div>
        <span className="text-xs text-gray-500">
          {loaded ? `${strategies.length} v2 strategies · ${history.length} session runs` : 'Loading...'}
        </span>
      </div>

      {loadError && (
        <p className="text-xs text-danger">{loadError}</p>
      )}

      {!loaded && <div className="h-24 bg-background rounded-lg animate-pulse" />}

      {/* A. Strategy picker */}
      {strategies.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {strategies.map(s => (
            <button
              key={s.id}
              onClick={() => pickStrategy(s)}
              className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition-colors ${
                selId === s.id
                  ? 'border-accent bg-accent/10 text-accent'
                  : 'border-border text-gray-300 hover:border-gray-500'
              }`}
            >
              <span className="font-medium font-mono">{s.id}</span>
              <span className="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-background text-gray-400 border border-border">
                {s.book_type}
              </span>
              {s.maker_only && (
                <span className="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider bg-warning/10 text-warning">
                  maker
                </span>
              )}
            </button>
          ))}
        </div>
      )}

      {/* B. Param form (auto-generated from schema) */}
      {sel && (
        <div className="p-4 bg-background rounded-lg border border-border space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-gray-300">
              Parameters <span className="text-gray-500 font-normal">({sel.params_schema.length})</span>
            </p>
            <button
              onClick={() => setParams(v2Defaults(sel))}
              className="text-xs text-accent hover:underline"
            >
              Reset to defaults
            </button>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-x-6 gap-y-4">
            {sel.params_schema.map(p => {
              const val = params[p.name] ?? p.default
              if (p.choices && p.choices.length > 0) {
                return (
                  <div key={p.name}>
                    <label className="block text-[11px] text-gray-400 mb-1 font-mono">{p.name}</label>
                    <select
                      value={String(val)}
                      onChange={e => {
                        const c = p.choices!.find(x => String(x) === e.target.value)
                        setParam(p.name, c ?? e.target.value)
                      }}
                      className="w-full bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent"
                    >
                      {p.choices.map(c => (
                        <option key={String(c)} value={String(c)}>{String(c)}</option>
                      ))}
                    </select>
                    {p.help && <p className="text-[10px] text-gray-500 mt-1">{p.help}</p>}
                  </div>
                )
              }
              if (p.type === 'bool') {
                const on = Boolean(val)
                return (
                  <div key={p.name}>
                    <label className="block text-[11px] text-gray-400 mb-1 font-mono">{p.name}</label>
                    <button
                      onClick={() => setParam(p.name, !on)}
                      className={`relative inline-flex h-5 w-10 items-center rounded-full transition-colors ${
                        on ? 'bg-accent' : 'bg-border'
                      }`}
                    >
                      <span
                        className="inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform"
                        style={{ transform: on ? 'translateX(22px)' : 'translateX(3px)' }}
                      />
                    </button>
                    {p.help && <p className="text-[10px] text-gray-500 mt-1">{p.help}</p>}
                  </div>
                )
              }
              // float / int → slider + numeric input pair
              const numVal = typeof val === 'number' ? val : parseFloat(String(val)) || p.default
              const isDefault = numVal === p.default
              return (
                <div key={p.name}>
                  <div className="flex items-center justify-between mb-1">
                    <label className="text-[11px] text-gray-400 font-mono" title={p.help || undefined}>
                      {p.name}
                    </label>
                    <span className={`text-[10px] font-mono ${isDefault ? 'text-gray-500' : 'text-accent'}`}>
                      {isDefault ? `default ${p.default}` : `default ${p.default} →`}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    {p.min != null && p.max != null && (
                      <input
                        type="range"
                        min={p.min}
                        max={p.max}
                        step={p.step ?? (p.type === 'int' ? 1 : 'any')}
                        value={numVal}
                        onChange={e =>
                          setParam(p.name, p.type === 'int' ? parseInt(e.target.value, 10) : parseFloat(e.target.value))
                        }
                        className="flex-1 h-1.5 accent-[#8b5cf6] cursor-pointer"
                      />
                    )}
                    <input
                      type="number"
                      min={p.min ?? undefined}
                      max={p.max ?? undefined}
                      step={p.step ?? undefined}
                      value={numVal}
                      onChange={e => {
                        const v = p.type === 'int' ? parseInt(e.target.value, 10) : parseFloat(e.target.value)
                        if (!isNaN(v)) setParam(p.name, v)
                      }}
                      className="w-20 bg-surface border border-border rounded px-2 py-1 text-xs text-gray-100 focus:outline-none focus:border-accent font-mono text-right"
                    />
                  </div>
                  {p.help && <p className="text-[10px] text-gray-500 mt-1">{p.help}</p>}
                </div>
              )
            })}
          </div>
        </div>
      )}

      {/* C. Data scope */}
      {sel && (
        <div className="p-4 bg-background rounded-lg border border-border space-y-3">
          <p className="text-sm font-medium text-gray-300">Data scope</p>
          <div className="flex flex-wrap gap-1.5">
            {symbolChoices.map(sym => {
              const on = symbols.includes(sym)
              const inUniverse = sel.universe.includes(sym)
              return (
                <button
                  key={sym}
                  onClick={() => toggleSymbol(sym)}
                  className={`px-2 py-1 rounded text-[11px] border font-mono transition-colors ${
                    on
                      ? 'border-accent text-accent bg-accent/10'
                      : 'border-border text-gray-500 hover:text-gray-300'
                  }`}
                  title={inUniverse ? 'strategy universe' : 'catalog'}
                >
                  {sym}
                  {inUniverse && <span className="ml-1 text-[9px] opacity-60">U</span>}
                </button>
              )
            })}
          </div>
          <div className="flex items-end gap-4 flex-wrap">
            <div>
              <label className="block text-[11px] text-gray-500 mb-1">Freq</label>
              <select
                value={freq}
                onChange={e => setFreq(e.target.value)}
                className="bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent"
              >
                {freqChoices.map(f => (
                  <option key={f} value={f}>
                    {f}{sel.primary_tf === f ? ' (primary)' : ''}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-[11px] text-gray-500 mb-1">Initial equity ($)</label>
              <input
                type="number"
                value={equity}
                onChange={e => setEquity(e.target.value)}
                className="w-28 bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent"
              />
            </div>
            <span className="text-[11px] text-gray-500 pb-1.5">
              {symbols.length} symbol{symbols.length === 1 ? '' : 's'} selected
            </span>
          </div>

          {/* Date range + per-symbol coverage bars */}
          <div className="pt-3 border-t border-border/50 space-y-3">
            <div className="flex items-end gap-4 flex-wrap">
              <div>
                <label className="block text-[11px] text-gray-500 mb-1">Date range</label>
                <div className="flex items-center gap-1">
                  {V2_RANGE_PRESETS.map(p => (
                    <button
                      key={p}
                      onClick={() => applyPreset(p)}
                      disabled={p !== 'Custom' && !coverage?.intersection}
                      className={`px-2 py-1 rounded text-[11px] border transition-colors ${
                        rangePreset === p
                          ? 'border-accent text-accent bg-accent/10'
                          : 'border-border text-gray-500 hover:text-gray-300 disabled:opacity-40 disabled:cursor-not-allowed'
                      }`}
                    >
                      {p}
                    </button>
                  ))}
                </div>
              </div>
              <div>
                <label className="block text-[11px] text-gray-500 mb-1">Start</label>
                <input
                  type="date"
                  value={startDate}
                  min={covAxis ? v2MsToISO(covAxis.start) : undefined}
                  max={endDate || (covAxis ? v2MsToISO(covAxis.end) : undefined)}
                  onChange={e => {
                    setStartDate(e.target.value)
                    setRangePreset('Custom')
                  }}
                  className="bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent font-mono [color-scheme:dark]"
                />
              </div>
              <div>
                <label className="block text-[11px] text-gray-500 mb-1">End</label>
                <input
                  type="date"
                  value={endDate}
                  min={startDate || (covAxis ? v2MsToISO(covAxis.start) : undefined)}
                  max={covAxis ? v2MsToISO(covAxis.end) : undefined}
                  onChange={e => {
                    setEndDate(e.target.value)
                    setRangePreset('Custom')
                  }}
                  className="bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent font-mono [color-scheme:dark]"
                />
              </div>
              {covLoading && <span className="text-[11px] text-gray-500 pb-1.5">loading coverage...</span>}
              {!covLoading && coverage?.intersection && (
                <span className="text-[11px] text-gray-500 pb-1.5 font-mono">
                  intersection {coverage.intersection.start} → {coverage.intersection.end}
                </span>
              )}
              {!covLoading && coverage && !coverage.intersection && (
                <span className="text-[11px] text-warning pb-1.5">no common window across symbols</span>
              )}
            </div>
            {coverage && covAxis && (
              <V2CoverageBars
                coverage={coverage}
                symbols={symbols}
                startDate={startDate}
                endDate={endDate}
                axis={covAxis}
              />
            )}
            {coverage && !covAxis && (
              <p className="text-[11px] text-gray-500">No coverage data for the selected symbols.</p>
            )}
          </div>
        </div>
      )}

      {/* Insufficient-data dialog (HTTP 422) */}
      {insufficient && (
        <div className="rounded-xl border border-rose-500/40 bg-rose-500/5 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-semibold text-rose-400">Insufficient data for requested window</p>
            <button
              onClick={() => setInsufficient(null)}
              className="text-gray-500 hover:text-gray-300 text-sm leading-none"
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
          <div className="space-y-2">
            {(insufficient.missing || []).map((m, i) => {
              const preListing = m.reason === 'does_not_exist_pre_listing'
              const notCollected =
                !preListing && m.reason !== 'not_in_catalog' && (m.reason === 'not_collected' || m.collectable)
              const badge = preListing
                ? { cls: 'bg-rose-500/15 text-rose-400 border-rose-500/40', label: '上市前数据不存在' }
                : notCollected
                ? { cls: 'bg-amber-500/15 text-amber-400 border-amber-500/40', label: '未采集 (可补)' }
                : { cls: 'bg-gray-500/15 text-gray-400 border-gray-500/40', label: '不在目录' }
              return (
                <div
                  key={i}
                  className="px-3 py-2 rounded-lg bg-background border border-border/50 text-xs space-y-1.5"
                >
                  <div className="flex items-center gap-3 flex-wrap">
                    <span className="font-mono text-gray-100 font-medium">{m.symbol}</span>
                    <span className={`px-1.5 py-0.5 rounded border text-[10px] font-medium whitespace-nowrap ${badge.cls}`}>
                      {badge.label}
                    </span>
                    <span className="text-gray-500 font-mono">
                      requested {m.requested?.[0]} → {m.requested?.[1]}
                      {m.have ? ` · have ${m.have[0]} → ${m.have[1]}` : ' · no data'}
                    </span>
                  </div>
                  {notCollected && m.hint && (
                    <div>
                      <p className="text-[10px] text-gray-500 mb-1">run this:</p>
                      <V2CopyCommand cmd={m.hint} />
                    </div>
                  )}
                  {!notCollected && m.hint && (
                    <p className="text-[11px] text-gray-400 font-mono break-all">{m.hint}</p>
                  )}
                </div>
              )
            })}
          </div>
          <div className="flex items-center justify-end gap-3">
            <button
              onClick={() => setInsufficient(null)}
              className="px-3 py-1.5 rounded-lg text-xs border border-border text-gray-400 hover:text-gray-200 transition-colors"
            >
              Dismiss
            </button>
            <button
              onClick={truncateToAvailable}
              disabled={!(insufficient.coverage?.intersection ?? coverage?.intersection)}
              className="px-3 py-1.5 rounded-lg text-xs font-medium bg-accent text-white hover:bg-accent/80 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              Truncate to available
            </button>
          </div>
        </div>
      )}

      {/* Mode tabs: Backtest | Sweep | Walk-Forward */}
      {sel && (
        <div className="flex items-center gap-1 border-b border-border">
          {([
            ['run', 'Backtest'],
            ['sweep', 'Sweep'],
            ['wf', 'Walk-Forward'],
          ] as Array<[V2Mode, string]>).map(([m, label]) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
                mode === m
                  ? 'border-accent text-accent'
                  : 'border-transparent text-gray-500 hover:text-gray-300'
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {/* Mode: Backtest */}
      {sel && mode === 'run' && (
        <div className="flex items-center justify-between flex-wrap gap-3">
          <span className="text-sm text-gray-500">{runMsg}</span>
          <button
            onClick={runV2}
            disabled={running || symbols.length === 0}
            className={`px-6 py-2 rounded-lg text-sm font-medium transition-colors ${
              running || symbols.length === 0
                ? 'bg-accent/50 text-white/50 cursor-not-allowed'
                : 'bg-accent text-white hover:bg-accent/80'
            }`}
          >
            {running ? (
              <span className="flex items-center gap-2">
                <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" fill="none" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z" />
                </svg>
                Running...
              </span>
            ) : 'Run Backtest'}
          </button>
        </div>
      )}

      {/* Mode: Sweep */}
      {sel && mode === 'sweep' && (
        <div className="p-4 bg-background rounded-lg border border-warning/30 space-y-3">
          <p className="text-sm font-medium text-warning">Parameter sweep</p>
          {renderParamSlots([
            { pick: sweepP1, setPick: setSweepP1, vals: sweepV1, setVals: setSweepV1, label: 'Param 1' },
            { pick: sweepP2, setPick: setSweepP2, vals: sweepV2, setVals: setSweepV2, label: 'Param 2 (optional)' },
          ])}
          <div className="flex items-center justify-between flex-wrap gap-2">
            <span className="text-xs text-gray-500">{sweepMsg}</span>
            <button
              onClick={runSweep}
              disabled={sweepBusy || !sweepP1}
              className={`px-4 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                sweepBusy || !sweepP1
                  ? 'bg-warning/30 text-white/50 cursor-not-allowed'
                  : 'bg-warning text-black hover:bg-warning/80'
              }`}
            >
              {sweepBusy ? `Sweeping... ${sweepState ? `${sweepState.done}/${sweepState.total}` : ''}` : 'Run Sweep'}
            </button>
          </div>
          {sweepState && sweepState.total > 0 && (
            <div className="h-1.5 bg-surface rounded-full overflow-hidden">
              <div
                className="h-full bg-warning transition-all"
                style={{ width: `${(sweepState.done / sweepState.total) * 100}%` }}
              />
            </div>
          )}
          {sortedSweepRows.length > 0 && isTwoParamSweep && (
            <div className="flex items-center gap-1">
              {(['heatmap', 'table'] as const).map(v => (
                <button
                  key={v}
                  onClick={() => setSweepView(v)}
                  className={`px-2.5 py-1 rounded text-[11px] border transition-colors capitalize ${
                    sweepView === v
                      ? 'border-warning text-warning bg-warning/10'
                      : 'border-border text-gray-500 hover:text-gray-300'
                  }`}
                >
                  {v}
                </button>
              ))}
            </div>
          )}
          {sortedSweepRows.length > 0 && isTwoParamSweep && sweepView === 'heatmap' && sweepState && (
            <V2SweepHeatmap
              rows={sweepState.results}
              keys={sweepKeys as [string, string]}
              onPick={applySweepRow}
            />
          )}
          {sortedSweepRows.length > 0 && !(isTwoParamSweep && sweepView === 'heatmap') && (
            <div className="overflow-x-auto">
              <p className="text-[11px] text-gray-500 mb-1.5">
                Sorted by Sharpe — click a row to apply its params to the form
              </p>
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-gray-500 border-b border-border">
                    {Object.keys(sortedSweepRows[0].params || {}).map(k => (
                      <th key={k} className="text-left pb-2 pr-3 font-mono">{k}</th>
                    ))}
                    <th className="text-right pb-2 pr-3">Sharpe</th>
                    <th className="text-right pb-2 pr-3">MaxDD</th>
                    <th className="text-right pb-2 pr-3">Return</th>
                    <th className="text-right pb-2 pr-3">Trades</th>
                    <th className="text-right pb-2 pr-3">WinRate</th>
                    <th className="text-right pb-2">PF</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedSweepRows.map((row, i) => (
                    <tr
                      key={i}
                      onClick={() => applySweepRow(row)}
                      className={`border-b border-border/30 transition-colors ${
                        row.error ? 'opacity-60' : 'cursor-pointer hover:bg-surface'
                      }`}
                    >
                      {Object.keys(sortedSweepRows[0].params || {}).map(k => (
                        <td key={k} className="py-1.5 pr-3 font-mono text-gray-300">
                          {String(row.params?.[k] ?? '')}
                        </td>
                      ))}
                      {row.error ? (
                        <td colSpan={6} className="py-1.5 text-danger text-[11px]">
                          {row.error}
                        </td>
                      ) : (
                        <>
                          <td className={`py-1.5 pr-3 text-right font-medium ${
                            (row.sharpe_ratio ?? 0) >= 1.5 ? 'text-success' : (row.sharpe_ratio ?? 0) >= 0 ? 'text-gray-100' : 'text-danger'
                          }`}>
                            {(row.sharpe_ratio ?? 0).toFixed(2)}
                          </td>
                          <td className="py-1.5 pr-3 text-right text-danger">
                            {v2AsPct(row.max_drawdown).toFixed(1)}%
                          </td>
                          <td className={`py-1.5 pr-3 text-right ${
                            (row.total_return_pct ?? 0) >= 0 ? 'text-success' : 'text-danger'
                          }`}>
                            {(row.total_return_pct ?? 0).toFixed(2)}%
                          </td>
                          <td className="py-1.5 pr-3 text-right text-gray-400">{row.total_trades ?? 0}</td>
                          <td className="py-1.5 pr-3 text-right text-gray-300">
                            {v2AsPct(row.win_rate).toFixed(1)}%
                          </td>
                          <td className="py-1.5 text-right text-gray-300">
                            {(row.profit_factor ?? 0).toFixed(2)}
                          </td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* Mode: Walk-Forward */}
      {sel && mode === 'wf' && (
        <div className="p-4 bg-background rounded-lg border border-accent/30 space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-2">
            <p className="text-sm font-medium text-gray-300">Walk-forward validation</p>
            <span className="text-[11px] text-gray-500">rolling train → test folds, stitched OOS equity, Sharpe gate 1.5</span>
          </div>
          <div className="flex items-end gap-4 flex-wrap">
            {([
              ['train_days', wfTrain, setWfTrain],
              ['test_days', wfTest, setWfTest],
              ['step_days', wfStep, setWfStep],
            ] as Array<[string, string, (v: string) => void]>).map(([label, val, setter]) => (
              <div key={label}>
                <label className="block text-[11px] text-gray-500 mb-1 font-mono">{label}</label>
                <input
                  type="number"
                  min={1}
                  value={val}
                  onChange={e => setter(e.target.value)}
                  className="w-20 bg-surface border border-border rounded px-2 py-1.5 text-xs text-gray-100 focus:outline-none focus:border-accent font-mono text-right"
                />
              </div>
            ))}
            <div className="pb-1">
              <label className="flex items-center gap-2 cursor-pointer select-none">
                <button
                  onClick={() => setWfOptimize(v => !v)}
                  className={`relative inline-flex h-5 w-10 items-center rounded-full transition-colors ${
                    wfOptimize ? 'bg-accent' : 'bg-border'
                  }`}
                >
                  <span
                    className="inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform"
                    style={{ transform: wfOptimize ? 'translateX(22px)' : 'translateX(3px)' }}
                  />
                </button>
                <span className="text-xs text-gray-400">optimize per fold</span>
              </label>
            </div>
          </div>
          {wfOptimize &&
            renderParamSlots([
              { pick: wfP1, setPick: setWfP1, vals: wfV1, setVals: setWfV1, label: 'Optimize param 1' },
              { pick: wfP2, setPick: setWfP2, vals: wfV2, setVals: setWfV2, label: 'Optimize param 2 (optional)' },
            ])}
          <div className="flex items-center justify-between flex-wrap gap-2">
            <span className="text-xs text-gray-500">{wfMsg}</span>
            <button
              onClick={runWalkforward}
              disabled={wfBusy || symbols.length === 0 || (wfOptimize && !wfP1)}
              className={`px-4 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                wfBusy || symbols.length === 0 || (wfOptimize && !wfP1)
                  ? 'bg-accent/30 text-white/50 cursor-not-allowed'
                  : 'bg-accent text-white hover:bg-accent/80'
              }`}
            >
              {wfBusy
                ? `Running... ${wfState ? `${wfState.folds_done}/${wfState.folds_total}` : ''}`
                : 'Run Walk-Forward'}
            </button>
          </div>

          {wfState && wfState.folds_total > 0 && wfState.status === 'running' && (
            <div className="h-1.5 bg-surface rounded-full overflow-hidden">
              <div
                className="h-full bg-accent transition-all"
                style={{ width: `${(wfState.folds_done / wfState.folds_total) * 100}%` }}
              />
            </div>
          )}

          {wfState?.status === 'failed' && (
            <div className="rounded-lg border border-danger/30 bg-danger/5 px-4 py-3 space-y-1">
              <p className="text-sm text-danger font-medium">Walk-forward failed</p>
              {wfState.error && <p className="text-xs text-gray-400 font-mono">{wfState.error}</p>}
              {wfState.detail && (
                <p className="text-xs text-gray-400">
                  {wfState.detail.have_days != null && wfState.detail.need_days_min != null && (
                    <>have {wfState.detail.have_days}d, need ≥ {wfState.detail.need_days_min}d. </>
                  )}
                  {wfState.detail.hint}
                </p>
              )}
            </div>
          )}

          {wfState?.status === 'completed' && wfState.gate && (
            <div
              className={`rounded-lg px-4 py-3 border ${
                wfState.gate.passed
                  ? 'border-success/40 bg-success/10'
                  : 'border-rose-500/40 bg-rose-500/10'
              }`}
            >
              <p className={`text-sm font-semibold ${wfState.gate.passed ? 'text-success' : 'text-rose-400'}`}>
                OOS Sharpe {(wfState.oos_sharpe ?? 0).toFixed(2)} vs gate {wfState.gate.threshold ?? 1.5} —{' '}
                {wfState.gate.passed ? 'PASSED' : 'FAILED'}
              </p>
              <p className="text-xs text-gray-400 mt-1">
                Pure out-of-sample across {wfState.folds_total} folds · stitched OOS return{' '}
                {(wfState.oos_return_pct ?? 0).toFixed(2)}%
              </p>
            </div>
          )}

          {wfState && wfState.folds && wfState.folds.length > 0 && (() => {
            const showBest = wfState.folds.some(
              f => f.best_params && Object.keys(f.best_params).length > 0
            )
            return (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="text-gray-500 border-b border-border">
                      <th className="text-left pb-2 pr-3">#</th>
                      <th className="text-left pb-2 pr-3">Train</th>
                      <th className="text-left pb-2 pr-3">Test</th>
                      {showBest && <th className="text-left pb-2 pr-3">Best params</th>}
                      <th className="text-right pb-2 pr-3">Train Sharpe</th>
                      <th className="text-right pb-2 pr-3">Test Sharpe</th>
                      <th className="text-right pb-2 pr-3">Return</th>
                      <th className="text-right pb-2 pr-3">MaxDD</th>
                      <th className="text-right pb-2">Trades</th>
                    </tr>
                  </thead>
                  <tbody>
                    {wfState.folds.map((f, i) => (
                      <tr key={i} className="border-b border-border/30">
                        <td className="py-1.5 pr-3 text-gray-500">{i + 1}</td>
                        <td className="py-1.5 pr-3 font-mono text-gray-400 whitespace-nowrap">
                          {String(f.train?.[0] ?? '').slice(0, 10)} → {String(f.train?.[1] ?? '').slice(0, 10)}
                        </td>
                        <td className="py-1.5 pr-3 font-mono text-gray-300 whitespace-nowrap">
                          {String(f.test?.[0] ?? '').slice(0, 10)} → {String(f.test?.[1] ?? '').slice(0, 10)}
                        </td>
                        {showBest && (
                          <td className="py-1.5 pr-3 font-mono text-gray-400">
                            {f.best_params ? v2ParamsDiff(f.best_params, {}) : '—'}
                          </td>
                        )}
                        <td className="py-1.5 pr-3 text-right text-gray-400">
                          {f.train_sharpe == null ? '—' : f.train_sharpe.toFixed(2)}
                        </td>
                        <td
                          className={`py-1.5 pr-3 text-right font-medium ${
                            (f.test_sharpe ?? 0) >= 1.5
                              ? 'text-success'
                              : (f.test_sharpe ?? 0) >= 0
                              ? 'text-gray-100'
                              : 'text-danger'
                          }`}
                        >
                          {f.test_sharpe == null ? '—' : f.test_sharpe.toFixed(2)}
                        </td>
                        <td
                          className={`py-1.5 pr-3 text-right ${
                            (f.test_return_pct ?? 0) >= 0 ? 'text-success' : 'text-danger'
                          }`}
                        >
                          {(f.test_return_pct ?? 0).toFixed(2)}%
                        </td>
                        <td className="py-1.5 pr-3 text-right text-danger">
                          {v2AsPct(f.test_max_dd).toFixed(1)}%
                        </td>
                        <td className="py-1.5 text-right text-gray-400">{f.test_trades ?? 0}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          })()}

          {wfState?.status === 'completed' &&
            Array.isArray(wfState.stitched_curve) &&
            wfState.stitched_curve.length > 1 && <V2StitchedCurve curve={wfState.stitched_curve} />}
        </div>
      )}

      {/* D. Active result */}
      {activeResult && activeResult.status === 'failed' && (
        <div className="rounded-lg border border-danger/30 bg-danger/5 px-4 py-3">
          <p className="text-sm text-danger font-medium">Run failed</p>
          <p className="text-xs text-gray-400 mt-1 font-mono">{activeResult.error}</p>
        </div>
      )}

      {activeResult && activeResult.status !== 'failed' && (
        <div className="space-y-4 pt-2 border-t border-border/50">
          <div className="flex items-center gap-2 flex-wrap">
            <h4 className="text-sm font-semibold text-gray-200">
              Result <span className="font-mono text-gray-500">{activeResult.backtest_id}</span>
            </h4>
            <span className="px-2 py-0.5 rounded text-[10px] font-medium bg-success/10 text-success">completed</span>
          </div>

          {/* Result tabs */}
          <div className="flex items-center gap-1 border-b border-border">
            {([
              ['overview', 'Overview'],
              ['tearsheet', 'Tear Sheet'],
              ...(Array.isArray(activeResult.trades) && activeResult.trades.length > 0
                ? ([['trades', `Trades (${activeResult.trades.length})`]] as Array<[V2ResultTab, string]>)
                : []),
            ] as Array<[V2ResultTab, string]>).map(([t, label]) => (
              <button
                key={t}
                onClick={() => setResultTab(t)}
                className={`px-3 py-1.5 text-xs font-medium border-b-2 -mb-px transition-colors ${
                  resultTab === t
                    ? 'border-accent text-accent'
                    : 'border-transparent text-gray-500 hover:text-gray-300'
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {/* Overview tab: KPI row + equity curve */}
          {resultTab === 'overview' && (
          <div className="space-y-4">
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            {[
              {
                label: 'Sharpe',
                value: (m2.sharpe_ratio ?? 0).toFixed(3),
                color: (m2.sharpe_ratio ?? 0) >= 1.5 ? 'text-success' : (m2.sharpe_ratio ?? 0) >= 0 ? 'text-warning' : 'text-danger',
              },
              { label: 'Max Drawdown', value: `${v2AsPct(m2.max_drawdown).toFixed(2)}%`, color: 'text-danger' },
              {
                label: 'Total Return',
                value: `${(m2.total_return_pct ?? 0).toFixed(2)}%`,
                color: (m2.total_return_pct ?? 0) >= 0 ? 'text-success' : 'text-danger',
              },
              { label: 'Trades', value: String(m2.total_trades ?? 0), color: 'text-gray-100' },
              { label: 'Win Rate', value: `${v2AsPct(m2.win_rate).toFixed(1)}%`, color: 'text-gray-100' },
              {
                label: 'Profit Factor',
                value: (m2.profit_factor ?? 0).toFixed(2),
                color: (m2.profit_factor ?? 0) >= 1 ? 'text-success' : 'text-danger',
              },
            ].map((kpi, i) => (
              <div key={i} className="bg-background rounded-lg p-3">
                <p className="text-xs text-gray-500 mb-1">{kpi.label}</p>
                <p className={`text-sm font-bold ${kpi.color}`}>{kpi.value}</p>
              </div>
            ))}
          </div>

          {/* Equity curve */}
          {Array.isArray(activeResult.equity_curve) && activeResult.equity_curve.length > 1 && (
            <div>
              <h5 className="text-xs font-semibold text-gray-400 mb-2">Equity Curve</h5>
              <div className="h-44 relative">
                <EquityCurveSVG
                  data={activeResult.equity_curve}
                  initialCash={activeResult.initial_capital ?? parseFloat(equity) ?? 5000}
                />
              </div>
            </div>
          )}
          </div>
          )}

          {/* Tear Sheet tab */}
          {resultTab === 'tearsheet' && (
            <TearSheet
              key={activeResult.backtest_id}
              analyticsUrl={`/api/backtest/results/${activeResult.backtest_id}/analytics`}
            />
          )}

          {/* Trades tab */}
          {resultTab === 'trades' && Array.isArray(activeResult.trades) && activeResult.trades.length > 0 && (
            <div>
              <p className="text-[11px] text-gray-500 mb-2">
                Last {Math.min(activeResult.trades.length, 50)} of {activeResult.trades.length}
              </p>
              {(
                <div className="overflow-x-auto max-h-72 overflow-y-auto mt-2">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-surface">
                      <tr className="text-gray-500 border-b border-border">
                        <th className="text-left pb-2 pr-4">Time</th>
                        <th className="text-left pb-2 pr-4">Symbol</th>
                        <th className="text-left pb-2 pr-4">Side</th>
                        <th className="text-right pb-2 pr-4">Price</th>
                        <th className="text-right pb-2 pr-4">Qty</th>
                        <th className="text-right pb-2">PnL</th>
                      </tr>
                    </thead>
                    <tbody>
                      {activeResult.trades.slice(-50).map((t: any, i: number) => {
                        const time = String(t.timestamp || t.ts || t.time || t.exit_time || '').substring(0, 19)
                        const side = String(t.side || t.action || t.direction || '')
                        const qty = Number(t.quantity ?? t.qty ?? t.size ?? 0)
                        const pnl = t.pnl ?? t.net_pnl ?? t.realized_pnl ?? null
                        const isBuy = /buy|long/i.test(side)
                        return (
                          <tr key={i} className="border-b border-border/30">
                            <td className="py-1.5 pr-4 text-gray-400">{time}</td>
                            <td className="py-1.5 pr-4 text-gray-100">{t.symbol || ''}</td>
                            <td className={`py-1.5 pr-4 font-medium ${isBuy ? 'text-success' : 'text-danger'}`}>{side}</td>
                            <td className="py-1.5 pr-4 text-right text-gray-100">
                              ${Number(t.price ?? t.entry_price ?? 0).toLocaleString(undefined, { maximumFractionDigits: 2 })}
                            </td>
                            <td className="py-1.5 pr-4 text-right text-gray-300">{qty.toFixed(4)}</td>
                            <td className={`py-1.5 text-right ${
                              pnl == null ? 'text-gray-500' : pnl >= 0 ? 'text-success' : 'text-danger'
                            }`}>
                              {pnl == null ? '—' : `$${Number(pnl).toFixed(2)}`}
                            </td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* D/E. Run history + compare */}
      {history.length > 0 && (
        <div className="pt-2 border-t border-border/50 space-y-3">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-gray-300">Run history (this session)</p>
            <span className="text-[11px] text-gray-500">check 2–4 runs to compare</span>
          </div>
          <div className="space-y-1.5">
            {history.map(h => (
              <div
                key={h.id}
                className="flex items-center gap-3 px-3 py-2 rounded-lg bg-background border border-border/50 text-xs flex-wrap"
              >
                <input
                  type="checkbox"
                  checked={compareIds.includes(h.id)}
                  disabled={h.status !== 'completed' && !compareIds.includes(h.id)}
                  onChange={() => toggleCompare(h.id)}
                  className="accent-[#8b5cf6] cursor-pointer"
                />
                <span className="font-mono text-gray-500">{h.id.substring(0, 14)}</span>
                <span className="text-gray-300 font-medium">{h.strategyId}</span>
                <span className="text-gray-500 font-mono">{h.paramsDiff}</span>
                <span className="flex-1" />
                {h.status === 'running' && <span className="text-warning">running...</span>}
                {h.status === 'failed' && (
                  <span className="text-danger truncate max-w-[300px]" title={h.error}>failed: {h.error}</span>
                )}
                {h.status === 'completed' && (
                  <span className={`font-bold ${
                    (h.sharpe ?? 0) >= 1.5 ? 'text-success' : (h.sharpe ?? 0) >= 0 ? 'text-gray-100' : 'text-danger'
                  }`}>
                    Sharpe {(h.sharpe ?? 0).toFixed(2)}
                  </span>
                )}
                <button
                  onClick={async () => {
                    const cached = resultCache[h.id]
                    if (cached) {
                      setActiveResult(cached)
                      return
                    }
                    try {
                      const res = await fetch(`${API}/api/backtest/results/${h.id}`)
                      if (res.ok) {
                        const data = await res.json()
                        setResultCache(prev => ({ ...prev, [h.id]: data }))
                        setActiveResult(data)
                      }
                    } catch {}
                  }}
                  className="text-accent hover:underline"
                >
                  view
                </button>
              </div>
            ))}
          </div>

          {/* E. Compare overlay */}
          {compareRuns.length >= 2 && (
            <V2CompareOverlay runs={compareRuns as Array<{ id: string; run: V2RunEntry; result: any }>} />
          )}
        </div>
      )}
    </div>
  )
}

function V2CoverageBars({
  coverage,
  symbols,
  startDate,
  endDate,
  axis,
}: {
  coverage: V2Coverage
  symbols: string[]
  startDate: string
  endDate: string
  axis: { start: number; end: number }
}) {
  const span = axis.end - axis.start || 1
  const pct = (ms: number) => Math.max(0, Math.min(100, ((ms - axis.start) / span) * 100))
  const winStart = startDate ? v2DateMs(startDate) : null
  const winEnd = endDate ? v2DateMs(endDate) : null
  const hasWin =
    winStart != null && winEnd != null && !isNaN(winStart) && !isNaN(winEnd) && winEnd >= winStart

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-[10px] text-gray-500 font-mono pl-16 pr-28">
        <span>{v2MsToISO(axis.start)}</span>
        <span>{v2MsToISO(axis.end)}</span>
      </div>
      {symbols.map(sym => {
        const s = coverage.symbols[sym] ?? null
        if (!s) {
          return (
            <div key={sym} className="flex items-center gap-2">
              <span className="w-14 text-[10px] font-mono text-gray-500 text-right shrink-0">{sym}</span>
              <div className="flex-1 h-4 rounded bg-gray-700/40 flex items-center justify-center">
                <span className="text-[9px] text-gray-500">not in catalog</span>
              </div>
              <span className="w-24 shrink-0" />
            </div>
          )
        }
        const sMs = v2DateMs(s.start)
        const eMs = v2DateMs(s.end)
        const left = pct(sMs)
        const width = Math.max(pct(eMs) - left, 0.5)
        let inLeft = 0
        let inWidth = 0
        const outSegs: Array<[number, number]> = []
        if (hasWin) {
          const iS = Math.max(winStart!, sMs)
          const iE = Math.min(winEnd!, eMs)
          if (iE > iS) {
            inLeft = pct(iS)
            inWidth = pct(iE) - inLeft
          }
          if (winStart! < sMs) outSegs.push([pct(winStart!), pct(Math.min(winEnd!, sMs))])
          if (winEnd! > eMs) outSegs.push([pct(Math.max(winStart!, eMs)), pct(winEnd!)])
        }
        return (
          <div key={sym} className="flex items-center gap-2">
            <span className="w-14 text-[10px] font-mono text-gray-300 text-right shrink-0">{sym}</span>
            <div
              className="flex-1 relative h-4 rounded bg-surface border border-border/50 overflow-hidden"
              title={`${s.start} → ${s.end} · ${s.rows ?? '?'} rows`}
            >
              {/* available span */}
              <div
                className="absolute inset-y-0 bg-accent/20"
                style={{ left: `${left}%`, width: `${width}%` }}
              />
              {/* selected window ∩ span → highlighted */}
              {hasWin && inWidth > 0 && (
                <div
                  className="absolute inset-y-0 bg-accent/60"
                  style={{ left: `${inLeft}%`, width: `${inWidth}%` }}
                />
              )}
              {/* selected window outside span → red */}
              {outSegs.map(([l, r], i) =>
                r > l ? (
                  <div
                    key={i}
                    className="absolute inset-y-0 bg-rose-500/50"
                    style={{ left: `${l}%`, width: `${r - l}%` }}
                  />
                ) : null
              )}
            </div>
            <span className="w-24 text-[9px] text-gray-500 font-mono shrink-0 text-right">
              {(s.rows ?? 0).toLocaleString()} rows
            </span>
          </div>
        )
      })}
      <div className="flex items-center gap-4 pl-16 text-[10px] text-gray-500">
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-2 rounded-sm bg-accent/20" /> available
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-2 rounded-sm bg-accent/60" /> selected window
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block w-3 h-2 rounded-sm bg-rose-500/50" /> window outside coverage
        </span>
      </div>
    </div>
  )
}

function V2StitchedCurve({ curve }: { curve: Array<{ timestamp: string; total_value: number }> }) {
  const w = 800
  const h = 200
  const base = curve[0]?.total_value || 1
  const vals = curve.map(p => ((p.total_value ?? 0) / base) * 100)
  let minV = 100
  let maxV = 100
  for (const v of vals) {
    if (v < minV) minV = v
    if (v > maxV) maxV = v
  }
  minV *= 0.998
  maxV *= 1.002
  const range = maxV - minV || 1
  const yOf = (v: number) => h - ((v - minV) / range) * h
  const baseY = yOf(100)
  const d = vals
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${((i / (vals.length - 1)) * w).toFixed(2)},${yOf(v).toFixed(2)}`)
    .join(' ')
  const last = vals[vals.length - 1]
  const color = last >= 100 ? '#10b981' : '#ef4444'

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-xs font-medium text-gray-300">Stitched OOS equity — rebased to 100</p>
        <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/10 text-accent font-medium">
          pure OOS, stitched
        </span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full">
        <line x1="0" y1={baseY} x2={w} y2={baseY} stroke="#374151" strokeWidth="1" strokeDasharray="4" />
        <text x="4" y={baseY - 4} fill="#6b7280" fontSize="10">100</text>
        <path d={d} fill="none" stroke={color} strokeWidth="1.75" />
      </svg>
      <div className="flex items-center justify-between text-[10px] text-gray-500 font-mono">
        <span>{String(curve[0].timestamp || '').slice(0, 10)}</span>
        <span>{String(curve[curve.length - 1].timestamp || '').slice(0, 10)}</span>
      </div>
    </div>
  )
}

function V2CompareOverlay({ runs }: { runs: Array<{ id: string; run: V2RunEntry; result: any }> }) {
  const w = 800
  const h = 240

  // Rebase each curve to 100 at its first point; plot by index % of length
  const series = runs.map((r, idx) => {
    const curve: Array<Record<string, any>> = r.result.equity_curve
    const base = v2EquityOf(curve[0]) || 1
    const vals = curve.map(p => (v2EquityOf(p) / base) * 100)
    return { id: r.id, label: r.run.paramsDiff, vals, color: V2_COLORS[idx % V2_COLORS.length] }
  })

  let minV = 100
  let maxV = 100
  for (const s of series) {
    for (const v of s.vals) {
      if (v < minV) minV = v
      if (v > maxV) maxV = v
    }
  }
  minV *= 0.998
  maxV *= 1.002
  const range = maxV - minV || 1
  const yOf = (v: number) => h - ((v - minV) / range) * h
  const baseY = yOf(100)

  return (
    <div className="p-4 bg-background rounded-lg border border-border space-y-2">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <p className="text-sm font-medium text-gray-300">Compare — equity rebased to 100</p>
        <span className="text-[10px] text-gray-500">
          index-aligned (x = % of each run&apos;s bar count, not wall-clock)
        </span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full">
        <line x1="0" y1={baseY} x2={w} y2={baseY} stroke="#374151" strokeWidth="1" strokeDasharray="4" />
        <text x="4" y={baseY - 4} fill="#6b7280" fontSize="10">100</text>
        {series.map(s => {
          const d = s.vals
            .map((v, i) => `${i === 0 ? 'M' : 'L'}${((i / (s.vals.length - 1)) * w).toFixed(2)},${yOf(v).toFixed(2)}`)
            .join(' ')
          return <path key={s.id} d={d} fill="none" stroke={s.color} strokeWidth="1.75" />
        })}
      </svg>
      <div className="flex items-center gap-4 flex-wrap">
        {series.map(s => (
          <span key={s.id} className="flex items-center gap-1.5 text-[11px] text-gray-400">
            <span className="inline-block w-4 h-0.5 rounded" style={{ backgroundColor: s.color }} />
            <span className="font-mono">{s.label}</span>
          </span>
        ))}
      </div>
      {/* Side-by-side metrics */}
      <div className="overflow-x-auto">
        <table className="w-full text-xs mt-1">
          <thead>
            <tr className="text-gray-500 border-b border-border">
              <th className="text-left pb-2 pr-4">Run</th>
              <th className="text-left pb-2 pr-4">Params</th>
              <th className="text-right pb-2 pr-4">Sharpe</th>
              <th className="text-right pb-2 pr-4">MaxDD</th>
              <th className="text-right pb-2 pr-4">Return</th>
              <th className="text-right pb-2 pr-4">Trades</th>
              <th className="text-right pb-2 pr-4">WinRate</th>
              <th className="text-right pb-2">PF</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((r, idx) => {
              const rm = r.result.metrics || {}
              return (
                <tr key={r.id} className="border-b border-border/30">
                  <td className="py-1.5 pr-4">
                    <span className="flex items-center gap-1.5">
                      <span
                        className="inline-block w-2 h-2 rounded-full"
                        style={{ backgroundColor: V2_COLORS[idx % V2_COLORS.length] }}
                      />
                      <span className="font-mono text-gray-500">{r.id.substring(0, 14)}</span>
                    </span>
                  </td>
                  <td className="py-1.5 pr-4 font-mono text-gray-400">{r.run.paramsDiff}</td>
                  <td className={`py-1.5 pr-4 text-right font-medium ${
                    (rm.sharpe_ratio ?? 0) >= 1.5 ? 'text-success' : (rm.sharpe_ratio ?? 0) >= 0 ? 'text-gray-100' : 'text-danger'
                  }`}>
                    {(rm.sharpe_ratio ?? 0).toFixed(2)}
                  </td>
                  <td className="py-1.5 pr-4 text-right text-danger">{v2AsPct(rm.max_drawdown).toFixed(1)}%</td>
                  <td className={`py-1.5 pr-4 text-right ${
                    (rm.total_return_pct ?? 0) >= 0 ? 'text-success' : 'text-danger'
                  }`}>
                    {(rm.total_return_pct ?? 0).toFixed(2)}%
                  </td>
                  <td className="py-1.5 pr-4 text-right text-gray-400">{rm.total_trades ?? 0}</td>
                  <td className="py-1.5 pr-4 text-right text-gray-300">{v2AsPct(rm.win_rate).toFixed(1)}%</td>
                  <td className="py-1.5 text-right text-gray-300">{(rm.profit_factor ?? 0).toFixed(2)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Sweep heatmap — 2-param grid, Sharpe colour-coded (green→red), click to apply
// ---------------------------------------------------------------------------

function V2SweepHeatmap({
  rows,
  keys,
  onPick,
}: {
  rows: V2SweepRow[]
  keys: [string, string]
  onPick: (row: V2SweepRow) => void
}) {
  const [rowKey, colKey] = keys
  const uniq = (k: string) =>
    Array.from(new Set(rows.map(r => r.params?.[k]))).sort(
      (a, b) => Number(a) - Number(b)
    )
  const rowVals = uniq(rowKey)
  const colVals = uniq(colKey)

  const cellFor = (rv: any, cv: any) =>
    rows.find(r => r.params?.[rowKey] === rv && r.params?.[colKey] === cv)

  // Normalise colour intensity to the largest |Sharpe| in the grid
  let maxAbs = 0
  for (const r of rows) {
    if (r.error) continue
    const s = Math.abs(r.sharpe_ratio ?? 0)
    if (s > maxAbs) maxAbs = s
  }
  maxAbs = maxAbs || 1

  const bgFor = (r: V2SweepRow | undefined): string => {
    if (!r || r.error || r.sharpe_ratio == null) return 'transparent'
    const s = r.sharpe_ratio
    const alpha = 0.12 + 0.68 * Math.min(1, Math.abs(s) / maxAbs)
    return s >= 0 ? `rgba(16,185,129,${alpha})` : `rgba(244,63,94,${alpha})`
  }

  return (
    <div className="space-y-1.5">
      <p className="text-[11px] text-gray-500">
        Sharpe heatmap — rows <span className="font-mono text-gray-400">{rowKey}</span>, cols{' '}
        <span className="font-mono text-gray-400">{colKey}</span>. Click a cell to apply both params.
      </p>
      <div className="overflow-x-auto">
        <table className="text-xs border-separate" style={{ borderSpacing: '3px' }}>
          <thead>
            <tr>
              <th className="p-1 text-right text-gray-500 font-mono">
                {rowKey}\{colKey}
              </th>
              {colVals.map(cv => (
                <th key={String(cv)} className="p-1 text-center text-gray-400 font-mono font-medium">
                  {String(cv)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rowVals.map(rv => (
              <tr key={String(rv)}>
                <td className="p-1 text-right text-gray-400 font-mono font-medium">{String(rv)}</td>
                {colVals.map(cv => {
                  const r = cellFor(rv, cv)
                  const disabled = !r || !!r.error || r.sharpe_ratio == null
                  return (
                    <td
                      key={String(cv)}
                      onClick={() => r && !r.error && onPick(r)}
                      title={
                        r
                          ? r.error
                            ? r.error
                            : `${rowKey}=${rv} ${colKey}=${cv} · Sharpe ${(r.sharpe_ratio ?? 0).toFixed(
                                2
                              )} · ret ${(r.total_return_pct ?? 0).toFixed(1)}% · ${r.total_trades ?? 0} trades`
                          : 'no result'
                      }
                      style={{ backgroundColor: bgFor(r) }}
                      className={`w-14 h-10 text-center rounded border border-border/40 font-mono transition-transform ${
                        disabled
                          ? 'text-gray-600 cursor-default'
                          : 'text-gray-100 cursor-pointer hover:scale-105'
                      }`}
                    >
                      {r && !r.error && r.sharpe_ratio != null ? r.sharpe_ratio.toFixed(2) : '—'}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// Copyable command box — used by the insufficient-data "Queue collection" hint.
function V2CopyCommand({ cmd }: { cmd: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(cmd)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {}
  }
  return (
    <div className="flex items-stretch gap-2">
      <code className="flex-1 bg-black/40 border border-border rounded px-2 py-1.5 text-[11px] text-gray-300 font-mono break-all">
        {cmd}
      </code>
      <button
        onClick={copy}
        className="px-2.5 py-1.5 rounded border border-border text-[11px] text-gray-400 hover:text-gray-200 hover:border-gray-500 transition-colors whitespace-nowrap"
      >
        {copied ? 'Copied' : 'Copy'}
      </button>
    </div>
  )
}
