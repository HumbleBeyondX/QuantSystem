'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8002'

interface LogFileInfo {
  name: string
  size_bytes: number
  size_human: string
  modified_at: string
  lines_estimate: number
}

interface TailResponse {
  filename: string
  lines: string[]
  total_returned: number
  truncated: boolean
}

interface ExportResponse {
  filename: string
  size_bytes: number
  download_url: string
}

const LEVEL_COLORS: Record<string, string> = {
  error: 'text-red-400',
  warning: 'text-yellow-400',
  warn: 'text-yellow-400',
  info: 'text-gray-300',
  debug: 'text-gray-500',
}

function extractLevel(line: string): string | null {
  try {
    const j = JSON.parse(line)
    if (typeof j.level === 'string') return j.level.toLowerCase()
  } catch {
    /* not JSON */
  }
  const m = line.match(/\[(error|warning|warn|info|debug)\s*\]/i)
  return m ? m[1].toLowerCase() : null
}

function formatLine(line: string): { prefix: string; message: string; level: string | null } {
  const level = extractLevel(line)
  try {
    const j = JSON.parse(line)
    const ts = j.timestamp || ''
    const event = j.event || j.message || ''
    const extras = Object.entries(j)
      .filter(([k]) => !['timestamp', 'event', 'message', 'level', 'logger'].includes(k))
      .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
      .join(' ')
    return {
      prefix: ts.slice(11, 19),
      message: `${event}${extras ? '  ' + extras : ''}`,
      level,
    }
  } catch {
    return { prefix: '', message: line, level }
  }
}

export default function LogsPage() {
  const [files, setFiles] = useState<LogFileInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [lines, setLines] = useState<string[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [levelFilter, setLevelFilter] = useState<string>('')
  const [autoRefresh, setAutoRefresh] = useState(true)
  const [tailLines, setTailLines] = useState(200)
  const [exporting, setExporting] = useState(false)
  const [exportResult, setExportResult] = useState<ExportResponse | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const fetchFiles = useCallback(async () => {
    try {
      const res = await fetch(`${API}/api/logs`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: LogFileInfo[] = await res.json()
      setFiles(data)
      if (!selected && data.length > 0) {
        setSelected(data[0].name)
      }
      setError(null)
    } catch (e: any) {
      setError(`Failed to list logs: ${e.message}`)
    }
  }, [selected])

  const fetchTail = useCallback(
    async (filename: string) => {
      setLoading(true)
      try {
        const params = new URLSearchParams({ filename, lines: String(tailLines) })
        if (levelFilter) params.set('level', levelFilter)
        const res = await fetch(`${API}/api/logs/tail?${params}`)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data: TailResponse = await res.json()
        setLines(data.lines)
        setError(null)
      } catch (e: any) {
        setError(`Tail failed: ${e.message}`)
      } finally {
        setLoading(false)
      }
    },
    [levelFilter, tailLines],
  )

  // Initial file list
  useEffect(() => {
    fetchFiles()
  }, [fetchFiles])

  // Tail whenever selection or filters change
  useEffect(() => {
    if (selected) fetchTail(selected)
  }, [selected, fetchTail])

  // Auto-refresh
  useEffect(() => {
    if (!autoRefresh || !selected) return
    const id = setInterval(() => {
      fetchTail(selected)
      fetchFiles()
    }, 3000)
    return () => clearInterval(id)
  }, [autoRefresh, selected, fetchTail, fetchFiles])

  // Scroll to bottom on new content
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [lines])

  const exportBundle = async () => {
    setExporting(true)
    setExportResult(null)
    try {
      const res = await fetch(`${API}/api/logs/export`, { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data: ExportResponse = await res.json()
      setExportResult(data)
    } catch (e: any) {
      setError(`Export failed: ${e.message}`)
    } finally {
      setExporting(false)
    }
  }

  const formatted = useMemo(() => lines.map(formatLine), [lines])

  return (
    <div className="flex gap-4 h-full">
      {/* File sidebar */}
      <aside className="w-72 shrink-0 space-y-3">
        <div className="bg-surface border border-border rounded-xl p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold text-gray-200">Log Files</h2>
            <button
              onClick={() => fetchFiles()}
              className="text-xs text-gray-500 hover:text-gray-300"
              title="Refresh"
            >
              ↻
            </button>
          </div>
          <div className="space-y-1 max-h-[60vh] overflow-auto">
            {files.length === 0 && (
              <p className="text-xs text-gray-500 py-2">No log files yet.</p>
            )}
            {files.map(f => (
              <button
                key={f.name}
                onClick={() => setSelected(f.name)}
                className={`w-full text-left px-3 py-2 rounded-lg text-xs transition-colors ${
                  selected === f.name
                    ? 'bg-blue-900/40 text-blue-200 border border-blue-700/50'
                    : 'text-gray-400 hover:bg-background hover:text-gray-200'
                }`}
              >
                <div className="font-mono truncate">{f.name}</div>
                <div className="text-[10px] text-gray-500 flex justify-between mt-1">
                  <span>{f.size_human}</span>
                  <span>{f.modified_at.slice(11, 16)}Z</span>
                </div>
              </button>
            ))}
          </div>
        </div>

        <div className="bg-surface border border-border rounded-xl p-4">
          <h2 className="text-sm font-semibold text-gray-200 mb-3">Support Bundle</h2>
          <p className="text-[11px] text-gray-500 mb-3">
            Zip all logs for troubleshooting.
          </p>
          <button
            onClick={exportBundle}
            disabled={exporting}
            className="w-full px-3 py-2 text-xs rounded-lg bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 text-white font-medium"
          >
            {exporting ? 'Building zip…' : 'Export bundle'}
          </button>
          {exportResult && (
            <div className="mt-3 text-[11px] text-gray-400">
              <a
                href={`${API}${exportResult.download_url}`}
                className="text-cyan-400 hover:underline break-all"
                target="_blank"
                rel="noopener noreferrer"
              >
                Download {exportResult.filename}
              </a>
              <div className="text-gray-600 mt-1">
                {(exportResult.size_bytes / 1024).toFixed(1)} KB
              </div>
            </div>
          )}
        </div>
      </aside>

      {/* Log tail viewer */}
      <section className="flex-1 flex flex-col bg-surface border border-border rounded-xl overflow-hidden">
        <header className="border-b border-border p-3 flex items-center gap-3 flex-wrap">
          <div className="flex-1 min-w-0">
            <p className="text-xs text-gray-500">File</p>
            <p className="text-sm font-mono text-gray-200 truncate">
              {selected ?? '—'}
            </p>
          </div>
          <select
            value={levelFilter}
            onChange={e => setLevelFilter(e.target.value)}
            className="bg-background text-gray-200 text-xs rounded-lg border border-border px-3 py-1.5"
          >
            <option value="">All levels</option>
            <option value="error">Error</option>
            <option value="warning">Warning</option>
            <option value="info">Info</option>
            <option value="debug">Debug</option>
          </select>
          <select
            value={tailLines}
            onChange={e => setTailLines(Number(e.target.value))}
            className="bg-background text-gray-200 text-xs rounded-lg border border-border px-3 py-1.5"
          >
            <option value={100}>100 lines</option>
            <option value={200}>200 lines</option>
            <option value={500}>500 lines</option>
            <option value={1000}>1000 lines</option>
          </select>
          <label className="flex items-center gap-2 text-xs text-gray-400">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={e => setAutoRefresh(e.target.checked)}
            />
            Auto-refresh (3s)
          </label>
          {selected && (
            <a
              href={`${API}/api/logs/download/${selected}`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-cyan-400 hover:underline"
            >
              Download raw
            </a>
          )}
        </header>

        {error && (
          <div className="bg-red-900/30 border-b border-red-800 p-2 text-red-300 text-xs">
            {error}
          </div>
        )}

        <div
          ref={scrollRef}
          className="flex-1 overflow-auto p-3 bg-background font-mono text-[11px] leading-relaxed"
        >
          {loading && lines.length === 0 && (
            <p className="text-gray-500">Loading…</p>
          )}
          {!loading && formatted.length === 0 && (
            <p className="text-gray-500">No log entries match the current filter.</p>
          )}
          {formatted.map((f, i) => (
            <div key={i} className="flex gap-2 whitespace-pre-wrap break-all">
              {f.prefix && <span className="text-gray-600 shrink-0">{f.prefix}</span>}
              <span className={f.level ? LEVEL_COLORS[f.level] ?? 'text-gray-300' : 'text-gray-300'}>
                {f.message}
              </span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}
