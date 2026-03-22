'use client'

import { useState, useEffect, useCallback, useRef } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL || 'http://localhost:8001'

/* ---------- types ---------- */
interface Quote {
  symbol: string
  price: number
  change: number
  change_pct: number
  open: number
  high: number
  low: number
  close: number
  volume: number
  prev_close: number
  bid: number | null
  ask: number | null
  bid_size: number | null
  ask_size: number | null
  timestamp: string | null
  source: string
}

interface Bar {
  timestamp: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  vwap?: number
}

interface Trade {
  timestamp: string
  price: number
  size: number
  conditions?: string[]
}

interface SearchResult {
  symbol: string
  name: string
}

/* ---------- default watchlist ---------- */
const DEFAULT_WATCHLIST = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'TSLA', 'NVDA', 'AMZN', 'META']
const TIMEFRAMES = ['1Min', '5Min', '15Min', '1Hour', '1Day'] as const
const TF_DAYS: Record<string, number> = { '1Min': 2, '5Min': 5, '15Min': 10, '1Hour': 30, '1Day': 90 }

/* ========== MAIN COMPONENT ========== */
export default function MarketPage() {
  const [selectedSymbol, setSelectedSymbol] = useState('SPY')
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResult[]>([])
  const [showSearch, setShowSearch] = useState(false)

  const [quote, setQuote] = useState<Quote | null>(null)
  const [watchlistQuotes, setWatchlistQuotes] = useState<Quote[]>([])
  const [bars, setBars] = useState<Bar[]>([])
  const [trades, setTrades] = useState<Trade[]>([])
  const [timeframe, setTimeframe] = useState<string>('1Day')

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdate, setLastUpdate] = useState<string>('')
  const searchRef = useRef<HTMLDivElement>(null)

  /* ----- fetch quote for selected symbol ----- */
  const fetchQuote = useCallback(async (sym: string) => {
    try {
      const r = await fetch(`${API}/api/market/quote/${sym}`)
      if (!r.ok) throw new Error(`Quote ${r.status}`)
      const data = await r.json()
      setQuote(data)
      setError(null)
    } catch (e: any) {
      setError(`Failed to fetch quote: ${e.message}`)
    }
  }, [])

  /* ----- fetch watchlist batch quotes ----- */
  const fetchWatchlist = useCallback(async () => {
    try {
      const r = await fetch(`${API}/api/market/quotes/batch?symbols=${DEFAULT_WATCHLIST.join(',')}`)
      if (!r.ok) throw new Error(`Watchlist ${r.status}`)
      const data = await r.json()
      setWatchlistQuotes(data.quotes || [])
    } catch (e: any) {
      console.error('Watchlist error:', e)
    }
  }, [])

  /* ----- fetch OHLCV bars ----- */
  const fetchBars = useCallback(async (sym: string, tf: string) => {
    try {
      const days = TF_DAYS[tf] || 30
      const r = await fetch(`${API}/api/market/ohlcv/${sym}?timeframe=${tf}&days=${days}&limit=200`)
      if (!r.ok) throw new Error(`OHLCV ${r.status}`)
      const data = await r.json()
      setBars(data || [])
    } catch (e: any) {
      console.error('OHLCV error:', e)
      setBars([])
    }
  }, [])

  /* ----- fetch recent trades ----- */
  const fetchTrades = useCallback(async (sym: string) => {
    try {
      const r = await fetch(`${API}/api/market/trades/${sym}?limit=30`)
      if (!r.ok) throw new Error(`Trades ${r.status}`)
      const data = await r.json()
      setTrades(data || [])
    } catch (e: any) {
      console.error('Trades error:', e)
      setTrades([])
    }
  }, [])

  /* ----- search symbols ----- */
  const handleSearch = useCallback(async (q: string) => {
    setSearchQuery(q)
    if (q.length < 1) { setSearchResults([]); setShowSearch(false); return }
    try {
      const r = await fetch(`${API}/api/market/search/${q}?limit=8`)
      if (!r.ok) return
      const data = await r.json()
      setSearchResults(data.results || [])
      setShowSearch(true)
    } catch { setSearchResults([]) }
  }, [])

  /* ----- select new symbol ----- */
  const selectSymbol = useCallback((sym: string) => {
    setSelectedSymbol(sym)
    setSearchQuery('')
    setShowSearch(false)
    setLoading(true)
  }, [])

  /* ----- initial load + refresh ----- */
  useEffect(() => {
    const loadAll = async () => {
      setLoading(true)
      await Promise.all([
        fetchQuote(selectedSymbol),
        fetchBars(selectedSymbol, timeframe),
        fetchTrades(selectedSymbol),
        fetchWatchlist(),
      ])
      setLastUpdate(new Date().toLocaleTimeString())
      setLoading(false)
    }
    loadAll()
    const interval = setInterval(loadAll, 15000) // refresh every 15s
    return () => clearInterval(interval)
  }, [selectedSymbol, timeframe, fetchQuote, fetchBars, fetchTrades, fetchWatchlist])

  /* ----- click outside search to close ----- */
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (searchRef.current && !searchRef.current.contains(e.target as Node)) setShowSearch(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const fmt = (n: number, d = 2) => n?.toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }) ?? '—'
  const fmtVol = (v: number) => {
    if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`
    if (v >= 1e6) return `${(v / 1e6).toFixed(2)}M`
    if (v >= 1e3) return `${(v / 1e3).toFixed(1)}K`
    return v.toString()
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Market Data</h2>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          {loading && <span className="animate-pulse text-accent">Updating...</span>}
          <span>Last: {lastUpdate}</span>
          <span className={`w-2 h-2 rounded-full ${error ? 'bg-red-500' : 'bg-green-500'}`} />
        </div>
      </div>

      {/* Search + Quick Pills */}
      <div className="flex flex-col md:flex-row gap-4">
        <div className="relative flex-1 max-w-md" ref={searchRef}>
          <input
            type="text"
            placeholder="Search symbol... (e.g. AAPL, SPY, TSLA)"
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            onFocus={() => searchQuery && setShowSearch(true)}
            className="w-full bg-surface border border-border rounded-lg pl-10 pr-4 py-2.5 text-sm text-gray-100 placeholder-gray-500 focus:outline-none focus:border-accent"
          />
          <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-500" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          {showSearch && searchResults.length > 0 && (
            <div className="absolute top-full mt-1 w-full bg-surface border border-border rounded-lg shadow-xl z-20 overflow-hidden">
              {searchResults.map((r) => (
                <button
                  key={r.symbol}
                  onClick={() => selectSymbol(r.symbol)}
                  className="w-full text-left px-4 py-2.5 text-sm hover:bg-background transition-colors flex justify-between items-center"
                >
                  <span className="text-gray-100 font-medium">{r.symbol}</span>
                  <span className="text-gray-500 text-xs truncate ml-2">{r.name}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="flex flex-wrap gap-2">
          {DEFAULT_WATCHLIST.slice(0, 6).map((sym) => (
            <button
              key={sym}
              onClick={() => selectSymbol(sym)}
              className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                selectedSymbol === sym
                  ? 'bg-accent text-white'
                  : 'bg-surface border border-border text-gray-400 hover:text-gray-100'
              }`}
            >
              {sym}
            </button>
          ))}
        </div>
      </div>

      {/* Price Header Card */}
      {quote && (
        <div className="bg-surface rounded-xl border border-border p-5 flex flex-wrap items-center gap-8">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="text-xl font-bold text-gray-100">{quote.symbol}</h3>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent/20 text-accent font-medium uppercase">{quote.source}</span>
            </div>
            <p className="text-3xl font-bold text-gray-100 mt-1 font-mono">
              ${fmt(quote.price)}
            </p>
          </div>
          <div className={quote.change >= 0 ? 'text-green-400' : 'text-red-400'}>
            <p className="text-lg font-semibold font-mono">
              {quote.change >= 0 ? '+' : ''}{fmt(quote.change)}
            </p>
            <p className="text-sm font-mono">
              {quote.change_pct >= 0 ? '+' : ''}{fmt(quote.change_pct)}%
            </p>
          </div>
          <div className="ml-auto flex flex-wrap gap-6 text-sm">
            <div>
              <p className="text-gray-500">Open</p>
              <p className="text-gray-100 font-mono">${fmt(quote.open)}</p>
            </div>
            <div>
              <p className="text-gray-500">High</p>
              <p className="text-gray-100 font-mono">${fmt(quote.high)}</p>
            </div>
            <div>
              <p className="text-gray-500">Low</p>
              <p className="text-gray-100 font-mono">${fmt(quote.low)}</p>
            </div>
            <div>
              <p className="text-gray-500">Prev Close</p>
              <p className="text-gray-100 font-mono">${fmt(quote.prev_close)}</p>
            </div>
            <div>
              <p className="text-gray-500">Volume</p>
              <p className="text-gray-100 font-mono">{fmtVol(quote.volume)}</p>
            </div>
            {quote.bid != null && quote.ask != null && (
              <div>
                <p className="text-gray-500">Bid / Ask</p>
                <p className="text-gray-100 font-mono text-xs">
                  ${fmt(quote.bid)} × {quote.bid_size ?? '—'} / ${fmt(quote.ask)} × {quote.ask_size ?? '—'}
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Price Chart */}
      <div className="bg-surface rounded-xl border border-border p-6">
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-lg font-semibold">
            Price Chart
            <span className="text-xs text-gray-500 ml-2 font-normal">{bars.length} bars</span>
          </h3>
          <div className="flex gap-1">
            {TIMEFRAMES.map((tf) => (
              <button
                key={tf}
                onClick={() => setTimeframe(tf)}
                className={`px-2.5 py-1 rounded text-xs transition-colors ${
                  timeframe === tf
                    ? 'bg-accent text-white'
                    : 'text-gray-400 hover:text-gray-100 hover:bg-background'
                }`}
              >
                {tf}
              </button>
            ))}
          </div>
        </div>
        <CandlestickChart bars={bars} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Watchlist */}
        <div className="bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">Watchlist</h3>
          <div className="grid grid-cols-4 text-xs text-gray-500 px-2 pb-2 border-b border-border">
            <span>Symbol</span>
            <span className="text-right">Price</span>
            <span className="text-right">Change</span>
            <span className="text-right">Volume</span>
          </div>
          <div className="divide-y divide-border/50">
            {watchlistQuotes.map((q) => (
              <button
                key={q.symbol}
                onClick={() => selectSymbol(q.symbol)}
                className={`w-full grid grid-cols-4 text-xs py-2.5 px-2 hover:bg-background/50 transition-colors ${
                  selectedSymbol === q.symbol ? 'bg-accent/5' : ''
                }`}
              >
                <span className="text-gray-100 font-medium text-left">{q.symbol}</span>
                <span className="text-right text-gray-100 font-mono">${fmt(q.price)}</span>
                <span className={`text-right font-mono ${q.change >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {q.change_pct >= 0 ? '+' : ''}{fmt(q.change_pct)}%
                </span>
                <span className="text-right text-gray-400 font-mono">{fmtVol(q.volume)}</span>
              </button>
            ))}
            {watchlistQuotes.length === 0 && (
              <div className="text-center text-gray-500 text-xs py-8">Loading watchlist...</div>
            )}
          </div>
        </div>

        {/* Recent Trades */}
        <div className="bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">
            Recent Trades
            <span className="text-xs text-gray-500 ml-2 font-normal">{quote?.symbol}</span>
          </h3>
          <div className="grid grid-cols-3 text-xs text-gray-500 px-2 pb-2 border-b border-border">
            <span>Time</span>
            <span className="text-right">Price</span>
            <span className="text-right">Size</span>
          </div>
          <div className="max-h-[400px] overflow-y-auto divide-y divide-border/30">
            {trades.map((t, i) => {
              const prevPrice = i < trades.length - 1 ? trades[i + 1]?.price : t.price
              const isBuy = t.price >= prevPrice
              return (
                <div key={i} className="grid grid-cols-3 text-xs py-1.5 px-2 hover:bg-background/50">
                  <span className="text-gray-500 font-mono">
                    {new Date(t.timestamp).toLocaleTimeString()}
                  </span>
                  <span className={`text-right font-mono ${isBuy ? 'text-green-400' : 'text-red-400'}`}>
                    ${fmt(t.price)}
                  </span>
                  <span className="text-right text-gray-400 font-mono">{t.size.toLocaleString()}</span>
                </div>
              )
            })}
            {trades.length === 0 && (
              <div className="text-center text-gray-500 text-xs py-8">
                {loading ? 'Loading trades...' : 'No trades available'}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Volume Chart */}
      {bars.length > 0 && (
        <div className="bg-surface rounded-xl border border-border p-6">
          <h3 className="text-lg font-semibold mb-4">Volume</h3>
          <VolumeChart bars={bars} />
        </div>
      )}
    </div>
  )
}


/* ========== CANDLESTICK CHART ========== */
function CandlestickChart({ bars }: { bars: Bar[] }) {
  if (bars.length === 0) {
    return (
      <div className="h-72 flex items-center justify-center text-gray-500 text-sm">
        No chart data available
      </div>
    )
  }

  const W = 900
  const H = 280
  const PAD = { top: 10, right: 60, bottom: 30, left: 10 }
  const chartW = W - PAD.left - PAD.right
  const chartH = H - PAD.top - PAD.bottom

  const prices = bars.flatMap(b => [b.high, b.low])
  const minP = Math.min(...prices)
  const maxP = Math.max(...prices)
  const range = maxP - minP || 1
  const padRange = range * 0.05

  const yScale = (p: number) => PAD.top + chartH - ((p - (minP - padRange)) / (range + 2 * padRange)) * chartH
  const barW = Math.max(1, (chartW / bars.length) * 0.7)
  const gap = chartW / bars.length

  // Y-axis grid lines
  const gridLines = 5
  const gridStep = (range + 2 * padRange) / gridLines

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-72" preserveAspectRatio="none">
      {/* Grid lines */}
      {Array.from({ length: gridLines + 1 }, (_, i) => {
        const price = (minP - padRange) + gridStep * i
        const y = yScale(price)
        return (
          <g key={i}>
            <line x1={PAD.left} y1={y} x2={W - PAD.right} y2={y}
              stroke="rgba(255,255,255,0.05)" strokeWidth={1} />
            <text x={W - PAD.right + 5} y={y + 4}
              fill="#6b7280" fontSize={10} fontFamily="monospace">
              ${price.toFixed(2)}
            </text>
          </g>
        )
      })}

      {/* Candlesticks */}
      {bars.map((b, i) => {
        const x = PAD.left + i * gap + gap / 2
        const isBull = b.close >= b.open
        const color = isBull ? '#10b981' : '#ef4444'
        const bodyTop = yScale(Math.max(b.open, b.close))
        const bodyBot = yScale(Math.min(b.open, b.close))
        const bodyH = Math.max(bodyBot - bodyTop, 1)

        return (
          <g key={i}>
            {/* Wick */}
            <line x1={x} y1={yScale(b.high)} x2={x} y2={yScale(b.low)}
              stroke={color} strokeWidth={1} />
            {/* Body */}
            <rect x={x - barW / 2} y={bodyTop} width={barW} height={bodyH}
              fill={isBull ? color : color} stroke={color} strokeWidth={0.5}
              rx={0.5} />
          </g>
        )
      })}

      {/* X-axis labels (show ~6 labels) */}
      {bars.filter((_, i) => i % Math.max(1, Math.floor(bars.length / 6)) === 0).map((b, idx) => {
        const origIdx = bars.indexOf(b)
        const x = PAD.left + origIdx * gap + gap / 2
        const d = new Date(b.timestamp)
        const label = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
        return (
          <text key={idx} x={x} y={H - 5}
            fill="#6b7280" fontSize={9} textAnchor="middle" fontFamily="monospace">
            {label}
          </text>
        )
      })}
    </svg>
  )
}


/* ========== VOLUME CHART ========== */
function VolumeChart({ bars }: { bars: Bar[] }) {
  const W = 900
  const H = 100
  const PAD = { left: 10, right: 60, top: 5, bottom: 5 }
  const chartW = W - PAD.left - PAD.right

  const maxVol = Math.max(...bars.map(b => b.volume))
  const gap = chartW / bars.length
  const barW = Math.max(1, gap * 0.7)

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-24" preserveAspectRatio="none">
      {bars.map((b, i) => {
        const x = PAD.left + i * gap + gap / 2
        const h = (b.volume / maxVol) * (H - PAD.top - PAD.bottom)
        const isBull = b.close >= b.open
        return (
          <rect key={i}
            x={x - barW / 2}
            y={H - PAD.bottom - h}
            width={barW}
            height={h}
            fill={isBull ? 'rgba(16,185,129,0.4)' : 'rgba(239,68,68,0.4)'}
            rx={0.5}
          />
        )
      })}
    </svg>
  )
}
