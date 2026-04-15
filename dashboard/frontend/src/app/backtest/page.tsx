'use client'

import { Suspense, useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'next/navigation'

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
        } else if (data.length > 0) {
          setSelectedStrategy(data[0].id)
          setSymbol(data[0].symbols[0] || 'BTC/USDT')
          setParams(data[0].default_params || {})
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
                {/* Group by asset class */}
                {['crypto', 'options', 'equity'].map(cls => {
                  const group = catalog.filter(s => s.asset_class === cls)
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
