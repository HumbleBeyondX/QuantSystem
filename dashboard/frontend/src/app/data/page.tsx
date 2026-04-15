'use client'

import { useState, useEffect, useCallback } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8002'
const MARKET_API = API.replace('/api', '') + '/api/market'

/**
 * Data Layer — Enterprise data management for quantitative strategies.
 *
 * Sections:
 * 1. Market Quotes — real-time price search (API passthrough, no storage)
 * 2. Data Sources — connector status and configuration
 * 3. Data Inventory — symbols, frequencies, storage tiers
 * 4. Collection — on-demand data fetch jobs
 * 5. Feature Store — computed technical/micro/macro features
 * 6. Pipeline — batch vs streaming architecture overview
 */

// ── Types ────────────────────────────────────────────────────

interface DataSource {
  id: string; name: string; type: string; connector_class: string
  status: string; data_types: string[]; supported_assets: string[]
  description: string; config: Record<string, any>
}

interface DatasetInfo {
  symbol: string; asset_class: string; source: string; data_type: string
  frequency: string; start_date: string | null; end_date: string | null
  records: number | null; storage_tier: string; size_mb: number | null; freshness: string
}

interface FeatureInfo {
  name: string; category: string; description: string
  parameters: Record<string, any>; frequency: string; dependencies: string[]
}

interface PipelineInfo {
  name: string; type: string; source: string; sink: string
  status: string; throughput: string | null; description: string
}

interface CollectionJob {
  id: string; symbol: string; source: string; data_type: string
  frequency: string; status: string; progress: number
  records_fetched: number; created_at: string; error: string | null
}

interface QuoteData {
  symbol: string; price: number; change: number; change_pct: number
  bid: number; ask: number; volume: number; high: number; low: number
}

type Tab = 'quotes' | 'sources' | 'inventory' | 'collection' | 'features' | 'pipeline'

// ── Main Component ────────────────────────────────────────────

export default function DataLayerPage() {
  const [tab, setTab] = useState<Tab>('sources')
  const [sources, setSources] = useState<DataSource[]>([])
  const [inventory, setInventory] = useState<DatasetInfo[]>([])
  const [features, setFeatures] = useState<FeatureInfo[]>([])
  const [pipelines, setPipelines] = useState<PipelineInfo[]>([])
  const [jobs, setJobs] = useState<CollectionJob[]>([])
  const [loading, setLoading] = useState(true)

  // Quotes state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<any[]>([])
  const [selectedQuote, setSelectedQuote] = useState<QuoteData | null>(null)
  const [watchlist, setWatchlist] = useState<QuoteData[]>([])

  // Collection form
  const [collectSymbol, setCollectSymbol] = useState('BTC/USDT')
  const [collectSource, setCollectSource] = useState('ccxt')
  const [collectFreq, setCollectFreq] = useState('1h')
  const [collectExchange, setCollectExchange] = useState('binance')
  const [collecting, setCollecting] = useState(false)

  // ── Fetch all data ────────────────────────────────────────

  const fetchAll = useCallback(async () => {
    try {
      const endpoints = [
        fetch(`${API}/api/data/sources`),
        fetch(`${API}/api/data/inventory`),
        fetch(`${API}/api/data/features`),
        fetch(`${API}/api/data/pipeline`),
        fetch(`${API}/api/data/jobs`),
      ]
      const [srcR, invR, featR, pipR, jobR] = await Promise.all(endpoints)
      if (srcR.ok) setSources(await srcR.json())
      if (invR.ok) setInventory(await invR.json())
      if (featR.ok) setFeatures(await featR.json())
      if (pipR.ok) setPipelines(await pipR.json())
      if (jobR.ok) setJobs(await jobR.json())
    } catch (e) { console.error('Data layer fetch error:', e) }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { fetchAll() }, [fetchAll])

  // ── Quote search ────────────────────────────────────────

  const searchSymbol = async () => {
    if (!searchQuery.trim()) return
    try {
      const res = await fetch(`${MARKET_API}/search/${searchQuery}`)
      if (res.ok) setSearchResults(await res.json())
    } catch { /* ignore */ }
  }

  const fetchQuote = async (symbol: string) => {
    try {
      const res = await fetch(`${MARKET_API}/quote/${symbol}`)
      if (res.ok) {
        const q = await res.json()
        setSelectedQuote(q)
      }
    } catch { /* ignore */ }
  }

  // ── Collection ────────────────────────────────────────

  const startCollection = async () => {
    setCollecting(true)
    try {
      const res = await fetch(`${API}/api/data/collect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: collectSymbol,
          source: collectSource,
          data_type: 'ohlcv',
          frequency: collectFreq,
          exchange: collectExchange,
        }),
      })
      if (res.ok) {
        const job = await res.json()
        setJobs((prev) => [job, ...prev])
        // Poll for completion
        setTimeout(async () => {
          const jr = await fetch(`${API}/api/data/jobs`)
          if (jr.ok) setJobs(await jr.json())
        }, 3000)
      }
    } catch (e: any) { alert(`Collection failed: ${e.message}`) }
    finally { setCollecting(false) }
  }

  // ── Derived ────────────────────────────────────────

  const onlineSources = sources.filter((s) => s.status === 'online').length
  const totalDatasets = inventory.length
  const totalFeatures = features.length
  const streamingPipes = pipelines.filter((p) => p.type === 'streaming').length
  const batchPipes = pipelines.filter((p) => p.type === 'batch').length

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: 'sources', label: 'Data Sources', count: sources.length },
    { id: 'inventory', label: 'Inventory', count: totalDatasets },
    { id: 'collection', label: 'Collection', count: jobs.length },
    { id: 'features', label: 'Feature Store', count: totalFeatures },
    { id: 'pipeline', label: 'Pipeline', count: pipelines.length },
    { id: 'quotes', label: 'Market Quotes' },
  ]

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-2xl font-bold">Data Layer</h2>
          <p className="text-xs text-gray-500 mt-1">
            Enterprise data management &mdash; sources, inventory, collection, features, pipeline
          </p>
        </div>
        <div className="flex items-center gap-4 text-xs">
          <Stat label="Sources Online" value={`${onlineSources}/${sources.length}`} color="text-success" />
          <Stat label="Datasets" value={totalDatasets.toString()} />
          <Stat label="Features" value={totalFeatures.toString()} />
          <Stat label="Pipelines" value={`${streamingPipes}S / ${batchPipes}B`} />
        </div>
      </div>

      {/* Tab Bar */}
      <div className="flex gap-1 bg-surface rounded-lg p-1 border border-border">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors flex items-center gap-2 ${
              tab === t.id ? 'bg-accent text-white' : 'text-gray-400 hover:text-gray-100 hover:bg-background'
            }`}
          >
            {t.label}
            {t.count !== undefined && (
              <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${
                tab === t.id ? 'bg-white/20' : 'bg-surface border border-border'
              }`}>
                {t.count}
              </span>
            )}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="space-y-4">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="bg-surface rounded-xl border border-border p-6 animate-pulse h-32" />
          ))}
        </div>
      ) : (
        <>
          {/* ── Data Sources Tab ───────────────── */}
          {tab === 'sources' && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
                {sources.map((src) => (
                  <div
                    key={src.id}
                    className={`bg-surface rounded-xl border p-5 transition-colors ${
                      src.status === 'online'
                        ? 'border-success/30'
                        : src.status === 'configured'
                          ? 'border-blue-500/30'
                          : 'border-border'
                    }`}
                  >
                    <div className="flex items-start justify-between mb-3">
                      <div>
                        <h3 className="font-semibold text-gray-100">{src.name}</h3>
                        <div className="flex items-center gap-2 mt-1">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium uppercase ${
                            src.type === 'streaming' ? 'bg-purple-500/20 text-purple-400' :
                            src.type === 'batch' ? 'bg-blue-500/20 text-blue-400' :
                            'bg-amber-500/20 text-amber-400'
                          }`}>
                            {src.type}
                          </span>
                          <span className="text-[10px] text-gray-500 font-mono">{src.connector_class}</span>
                        </div>
                      </div>
                      <StatusBadge status={src.status} />
                    </div>
                    <p className="text-xs text-gray-500 mb-3 line-clamp-2">{src.description}</p>
                    <div className="flex flex-wrap gap-1 mb-2">
                      {src.data_types.map((dt) => (
                        <span key={dt} className="text-[10px] px-1.5 py-0.5 rounded bg-background border border-border text-gray-400">
                          {dt}
                        </span>
                      ))}
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {src.supported_assets.map((a) => (
                        <span key={a} className="text-[10px] px-1.5 py-0.5 rounded bg-accent/10 text-accent">
                          {a}
                        </span>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Data Inventory Tab ───────────────── */}
          {tab === 'inventory' && (
            <div className="bg-surface rounded-xl border border-border overflow-hidden">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-gray-400 border-b border-border bg-background/50">
                      <th className="text-left px-4 py-3 font-medium">Symbol</th>
                      <th className="text-left px-4 py-3 font-medium">Asset Class</th>
                      <th className="text-left px-4 py-3 font-medium">Source</th>
                      <th className="text-left px-4 py-3 font-medium">Type</th>
                      <th className="text-left px-4 py-3 font-medium">Frequency</th>
                      <th className="text-left px-4 py-3 font-medium">Storage</th>
                      <th className="text-right px-4 py-3 font-medium">Records</th>
                      <th className="text-right px-4 py-3 font-medium">Size</th>
                      <th className="text-left px-4 py-3 font-medium">Freshness</th>
                    </tr>
                  </thead>
                  <tbody>
                    {inventory.map((d, i) => (
                      <tr key={i} className="border-b border-border/50 hover:bg-background/30 transition-colors">
                        <td className="px-4 py-3 font-mono font-medium text-gray-100">{d.symbol}</td>
                        <td className="px-4 py-3">
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                            d.asset_class === 'crypto' ? 'bg-purple-500/20 text-purple-400' :
                            d.asset_class === 'equity' ? 'bg-blue-500/20 text-blue-400' :
                            d.asset_class === 'options' ? 'bg-amber-500/20 text-amber-400' :
                            'bg-gray-500/20 text-gray-400'
                          }`}>
                            {d.asset_class}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-gray-400 text-xs">{d.source}</td>
                        <td className="px-4 py-3 text-gray-400 text-xs">{d.data_type}</td>
                        <td className="px-4 py-3">
                          <span className="text-xs font-mono text-gray-300">{d.frequency}</span>
                        </td>
                        <td className="px-4 py-3">
                          <StorageBadge tier={d.storage_tier} />
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-xs text-gray-400">
                          {d.records?.toLocaleString() ?? '\u2014'}
                        </td>
                        <td className="px-4 py-3 text-right font-mono text-xs text-gray-400">
                          {d.size_mb ? `${d.size_mb} MB` : '\u2014'}
                        </td>
                        <td className="px-4 py-3">
                          <FreshnessBadge freshness={d.freshness} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* ── Collection Tab ───────────────── */}
          {tab === 'collection' && (
            <div className="space-y-6">
              {/* Collection Form */}
              <div className="bg-surface rounded-xl border border-border p-6">
                <h3 className="text-lg font-semibold mb-4">Start New Collection</h3>
                <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
                  <div>
                    <label className="text-xs text-gray-400 block mb-1.5">Symbol</label>
                    <input
                      value={collectSymbol}
                      onChange={(e) => setCollectSymbol(e.target.value)}
                      placeholder="BTC/USDT"
                      className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100 font-mono"
                    />
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1.5">Source</label>
                    <select
                      value={collectSource}
                      onChange={(e) => setCollectSource(e.target.value)}
                      className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100"
                    >
                      <option value="ccxt">CCXT (Crypto)</option>
                      <option value="yfinance">Yahoo Finance (Equity)</option>
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1.5">Frequency</label>
                    <select
                      value={collectFreq}
                      onChange={(e) => setCollectFreq(e.target.value)}
                      className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100"
                    >
                      <option value="1m">1 Minute</option>
                      <option value="5m">5 Minutes</option>
                      <option value="15m">15 Minutes</option>
                      <option value="1h">1 Hour</option>
                      <option value="4h">4 Hours</option>
                      <option value="1d">1 Day</option>
                    </select>
                  </div>
                  <div>
                    <label className="text-xs text-gray-400 block mb-1.5">Exchange</label>
                    <select
                      value={collectExchange}
                      onChange={(e) => setCollectExchange(e.target.value)}
                      className="w-full bg-background border border-border rounded-lg px-3 py-2 text-sm text-gray-100"
                    >
                      <option value="binance">Binance</option>
                      <option value="okx">OKX</option>
                      <option value="bybit">Bybit</option>
                    </select>
                  </div>
                  <div className="flex items-end">
                    <button
                      onClick={startCollection}
                      disabled={collecting}
                      className="w-full px-4 py-2 bg-accent hover:bg-accent/80 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
                    >
                      {collecting ? 'Starting...' : 'Collect Data'}
                    </button>
                  </div>
                </div>
              </div>

              {/* Jobs List */}
              <div className="bg-surface rounded-xl border border-border p-6">
                <h3 className="text-lg font-semibold mb-4">Collection Jobs</h3>
                {jobs.length > 0 ? (
                  <div className="space-y-3">
                    {jobs.map((job) => (
                      <div
                        key={job.id}
                        className={`bg-background rounded-lg border p-4 ${
                          job.status === 'completed' ? 'border-success/30' :
                          job.status === 'failed' ? 'border-danger/30' :
                          job.status === 'running' ? 'border-accent/30' :
                          'border-border'
                        }`}
                      >
                        <div className="flex items-center justify-between">
                          <div className="flex items-center gap-3">
                            <span className={`w-2 h-2 rounded-full ${
                              job.status === 'completed' ? 'bg-success' :
                              job.status === 'failed' ? 'bg-danger' :
                              job.status === 'running' ? 'bg-accent animate-pulse' :
                              'bg-gray-500'
                            }`} />
                            <div>
                              <span className="text-sm font-mono font-medium text-gray-100">{job.symbol}</span>
                              <span className="text-xs text-gray-500 ml-2">
                                {job.source} &middot; {job.frequency} &middot; {job.data_type}
                              </span>
                            </div>
                          </div>
                          <div className="flex items-center gap-4 text-xs">
                            {job.records_fetched > 0 && (
                              <span className="text-gray-400 font-mono">{job.records_fetched.toLocaleString()} records</span>
                            )}
                            <span className={`px-2 py-0.5 rounded font-medium uppercase ${
                              job.status === 'completed' ? 'bg-success/10 text-success' :
                              job.status === 'failed' ? 'bg-danger/10 text-danger' :
                              job.status === 'running' ? 'bg-accent/10 text-accent' :
                              'bg-gray-500/10 text-gray-400'
                            }`}>
                              {job.status}
                            </span>
                          </div>
                        </div>
                        {job.error && (
                          <p className="text-xs text-danger mt-2 ml-5">{job.error}</p>
                        )}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-center text-gray-500 text-sm py-8">
                    No collection jobs yet. Start one above.
                  </p>
                )}
              </div>
            </div>
          )}

          {/* ── Feature Store Tab ───────────────── */}
          {tab === 'features' && (
            <div className="space-y-4">
              {['technical', 'microstructure', 'macro', 'sentiment'].map((category) => {
                const catFeatures = features.filter((f) => f.category === category)
                if (catFeatures.length === 0) return null
                return (
                  <div key={category} className="bg-surface rounded-xl border border-border p-6">
                    <h3 className="text-sm font-semibold text-gray-400 uppercase tracking-wider mb-4">
                      {category} Features
                      <span className="text-xs text-gray-600 ml-2 normal-case">({catFeatures.length})</span>
                    </h3>
                    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                      {catFeatures.map((feat) => (
                        <div key={feat.name} className="bg-background rounded-lg border border-border p-4">
                          <div className="flex items-center justify-between mb-2">
                            <span className="font-mono font-semibold text-sm text-gray-100">{feat.name}</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface text-gray-500 font-mono">
                              {feat.frequency}
                            </span>
                          </div>
                          <p className="text-xs text-gray-500 mb-3">{feat.description}</p>
                          <div className="flex flex-wrap gap-1">
                            {Object.entries(feat.parameters).map(([k, v]) => (
                              <span key={k} className="text-[10px] px-1.5 py-0.5 rounded bg-surface border border-border text-gray-400 font-mono">
                                {k}={typeof v === 'object' ? JSON.stringify(v) : String(v)}
                              </span>
                            ))}
                          </div>
                          <div className="flex gap-1 mt-2">
                            {feat.dependencies.map((dep) => (
                              <span key={dep} className="text-[10px] px-1.5 py-0.5 rounded bg-accent/10 text-accent">
                                requires: {dep}
                              </span>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {/* ── Pipeline Tab ───────────────── */}
          {tab === 'pipeline' && (
            <div className="space-y-4">
              {/* Architecture Diagram */}
              <div className="bg-surface rounded-xl border border-border p-6">
                <h3 className="text-lg font-semibold mb-4">Data Architecture</h3>
                <div className="grid grid-cols-5 gap-3 text-center text-xs">
                  <ArchBlock title="Sources" items={['CCXT WS', 'Alpaca WS', 'Polygon', 'IB', 'FRED']} color="purple" />
                  <ArchBlock title="Ingestion" items={['Validate', 'Normalize', 'Deduplicate']} color="blue" />
                  <ArchBlock title="Message Bus" items={['Kafka / Redpanda', 'Topic per asset']} color="amber" />
                  <ArchBlock title="Storage" items={['Redis (hot)', 'TimescaleDB (warm)', 'Parquet/S3 (cold)']} color="green" />
                  <ArchBlock title="Consumers" items={['Strategies', 'Feature Engine', 'Dashboard', 'Alerts']} color="red" />
                </div>
                <div className="flex justify-center mt-3">
                  <div className="flex items-center gap-2 text-[10px] text-gray-500">
                    <span className="w-8 h-px bg-gray-600" />
                    <span>Streaming (real-time)</span>
                    <span className="w-8 h-px bg-gray-600 border-dashed" style={{ borderTop: '1px dashed #4b5563' }} />
                    <span>Batch (scheduled)</span>
                  </div>
                </div>
              </div>

              {/* Pipeline Cards */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {pipelines.map((pipe) => (
                  <div
                    key={pipe.name}
                    className={`bg-surface rounded-xl border p-5 ${
                      pipe.type === 'streaming' ? 'border-purple-500/20' : 'border-blue-500/20'
                    }`}
                  >
                    <div className="flex items-center justify-between mb-3">
                      <div className="flex items-center gap-2">
                        <h4 className="font-semibold text-sm text-gray-100">{pipe.name}</h4>
                        <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium uppercase ${
                          pipe.type === 'streaming' ? 'bg-purple-500/20 text-purple-400' : 'bg-blue-500/20 text-blue-400'
                        }`}>
                          {pipe.type}
                        </span>
                      </div>
                      <StatusBadge status={pipe.status} />
                    </div>
                    <p className="text-xs text-gray-500 mb-3">{pipe.description}</p>
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      <div>
                        <p className="text-gray-600">Source</p>
                        <p className="text-gray-300 font-mono text-[11px]">{pipe.source}</p>
                      </div>
                      <div>
                        <p className="text-gray-600">Sink</p>
                        <p className="text-gray-300 font-mono text-[11px]">{pipe.sink}</p>
                      </div>
                    </div>
                    {pipe.throughput && (
                      <p className="text-[10px] text-gray-500 mt-2 font-mono">Throughput: {pipe.throughput}</p>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Market Quotes Tab ───────────────── */}
          {tab === 'quotes' && (
            <div className="space-y-4">
              <div className="bg-surface rounded-xl border border-border p-6">
                <h3 className="text-lg font-semibold mb-4">Real-Time Market Quotes</h3>
                <div className="flex gap-3 mb-4">
                  <input
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && searchSymbol()}
                    placeholder="Search symbol (e.g. AAPL, BTC, SPY...)"
                    className="flex-1 bg-background border border-border rounded-lg px-4 py-2.5 text-sm text-gray-100"
                  />
                  <button
                    onClick={searchSymbol}
                    className="px-6 py-2.5 bg-accent hover:bg-accent/80 text-white rounded-lg text-sm font-medium transition-colors"
                  >
                    Search
                  </button>
                </div>

                {searchResults.length > 0 && (
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mb-4">
                    {searchResults.slice(0, 8).map((r: any) => (
                      <button
                        key={r.symbol}
                        onClick={() => fetchQuote(r.symbol)}
                        className="bg-background border border-border rounded-lg p-3 text-left hover:border-accent/50 transition-colors"
                      >
                        <p className="font-mono font-semibold text-sm text-gray-100">{r.symbol}</p>
                        <p className="text-[10px] text-gray-500 truncate">{r.name}</p>
                      </button>
                    ))}
                  </div>
                )}

                {selectedQuote && (
                  <div className="bg-background rounded-lg border border-border p-5">
                    <div className="flex items-center justify-between mb-4">
                      <h4 className="text-xl font-bold font-mono">{selectedQuote.symbol}</h4>
                      <span className={`text-2xl font-bold font-mono ${
                        selectedQuote.change >= 0 ? 'text-success' : 'text-danger'
                      }`}>
                        ${selectedQuote.price?.toFixed(2)}
                      </span>
                    </div>
                    <div className="grid grid-cols-3 md:grid-cols-6 gap-4 text-xs">
                      <div>
                        <p className="text-gray-500">Change</p>
                        <p className={`font-mono font-medium ${selectedQuote.change >= 0 ? 'text-success' : 'text-danger'}`}>
                          {selectedQuote.change >= 0 ? '+' : ''}{selectedQuote.change?.toFixed(2)}
                          ({selectedQuote.change_pct?.toFixed(2)}%)
                        </p>
                      </div>
                      <div>
                        <p className="text-gray-500">Bid</p>
                        <p className="font-mono text-gray-100">${selectedQuote.bid?.toFixed(2)}</p>
                      </div>
                      <div>
                        <p className="text-gray-500">Ask</p>
                        <p className="font-mono text-gray-100">${selectedQuote.ask?.toFixed(2)}</p>
                      </div>
                      <div>
                        <p className="text-gray-500">High</p>
                        <p className="font-mono text-gray-100">${selectedQuote.high?.toFixed(2)}</p>
                      </div>
                      <div>
                        <p className="text-gray-500">Low</p>
                        <p className="font-mono text-gray-100">${selectedQuote.low?.toFixed(2)}</p>
                      </div>
                      <div>
                        <p className="text-gray-500">Volume</p>
                        <p className="font-mono text-gray-100">{selectedQuote.volume?.toLocaleString()}</p>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}


// ── Utility Components ────────────────────────────────────────

function Stat({ label, value, color = 'text-gray-100' }: { label: string; value: string; color?: string }) {
  return (
    <div className="text-center">
      <p className="text-gray-500">{label}</p>
      <p className={`font-mono font-semibold ${color}`}>{value}</p>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    online: 'bg-success/10 text-success',
    configured: 'bg-blue-500/10 text-blue-400',
    available: 'bg-blue-500/10 text-blue-400',
    missing_keys: 'bg-warning/10 text-warning',
    offline: 'bg-danger/10 text-danger',
  }
  return (
    <span className={`text-[10px] px-2 py-0.5 rounded-full font-medium uppercase ${styles[status] || 'bg-gray-500/10 text-gray-400'}`}>
      {status.replace('_', ' ')}
    </span>
  )
}

function StorageBadge({ tier }: { tier: string }) {
  const styles: Record<string, string> = {
    hot: 'bg-red-500/10 text-red-400',
    warm: 'bg-amber-500/10 text-amber-400',
    cold: 'bg-blue-500/10 text-blue-400',
  }
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded font-mono ${styles[tier] || 'bg-gray-500/10 text-gray-400'}`}>
      {tier}
    </span>
  )
}

function FreshnessBadge({ freshness }: { freshness: string }) {
  const styles: Record<string, string> = {
    live: 'text-success',
    '< 1h': 'text-success',
    '< 1d': 'text-amber-400',
    static: 'text-blue-400',
    archived: 'text-blue-400',
    stale: 'text-danger',
    unknown: 'text-gray-500',
  }
  return <span className={`text-xs font-mono ${styles[freshness] || 'text-gray-500'}`}>{freshness}</span>
}

function ArchBlock({ title, items, color }: { title: string; items: string[]; color: string }) {
  const borderColor: Record<string, string> = {
    purple: 'border-purple-500/30',
    blue: 'border-blue-500/30',
    amber: 'border-amber-500/30',
    green: 'border-green-500/30',
    red: 'border-red-500/30',
  }
  const titleColor: Record<string, string> = {
    purple: 'text-purple-400',
    blue: 'text-blue-400',
    amber: 'text-amber-400',
    green: 'text-green-400',
    red: 'text-red-400',
  }
  return (
    <div className={`bg-background rounded-lg border ${borderColor[color]} p-3`}>
      <p className={`font-semibold mb-2 ${titleColor[color]}`}>{title}</p>
      {items.map((item) => (
        <p key={item} className="text-gray-500 text-[10px] leading-relaxed">{item}</p>
      ))}
    </div>
  )
}
