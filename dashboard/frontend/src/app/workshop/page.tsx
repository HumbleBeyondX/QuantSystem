'use client'

import dynamic from 'next/dynamic'
import Link from 'next/link'
import { useCallback, useEffect, useRef, useState } from 'react'

const MonacoEditor = dynamic(() => import('@monaco-editor/react'), { ssr: false })

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://localhost:8001'

// ── Types ────────────────────────────────────────────────────

interface Template {
  name: string
  description: string
  base_class: string
  asset_classes: string[]
  default_symbols: string[]
  default_params: Record<string, any>
}

interface StrategyItem {
  strategy_id: string
  class_name: string
  base_class: string
  category: string
  file_path: string
  description: string
  has_config: boolean
  lines: number
}

interface ValidationResult {
  valid: boolean
  errors: string[]
  warnings: string[]
  class_name: string | null
  base_class: string | null
  has_generate_signals: boolean
}

// ── Helpers ──────────────────────────────────────────────────

async function fetchJSON(path: string, opts?: RequestInit) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || err.message || res.statusText)
  }
  return res.json()
}

// ── Asset class colors ───────────────────────────────────────

function assetBadge(category: string) {
  const colors: Record<string, string> = {
    crypto: 'bg-purple-500/20 text-purple-400',
    equity: 'bg-blue-500/20 text-blue-400',
    options: 'bg-amber-500/20 text-amber-400',
  }
  return colors[category] || 'bg-gray-500/20 text-gray-400'
}

function baseBadge(base: string) {
  const colors: Record<string, string> = {
    StrategyBase: 'bg-gray-600/20 text-gray-400',
    OptionsStrategyBase: 'bg-amber-600/20 text-amber-300',
    FuturesStrategyBase: 'bg-emerald-600/20 text-emerald-300',
    HFTStrategyBase: 'bg-rose-600/20 text-rose-300',
  }
  return colors[base] || 'bg-gray-600/20 text-gray-400'
}

// ── Main Page Component ──────────────────────────────────────

export default function WorkshopPage() {
  // ── State ──
  const [view, setView] = useState<'list' | 'editor' | 'create'>('list')
  const [strategies, setStrategies] = useState<StrategyItem[]>([])
  const [templates, setTemplates] = useState<Template[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Editor state
  const [activeStrategy, setActiveStrategy] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [config, setConfig] = useState('')
  const [editorTab, setEditorTab] = useState<'code' | 'config'>('code')
  const [saving, setSaving] = useState(false)
  const [validation, setValidation] = useState<ValidationResult | null>(null)
  const [validating, setValidating] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)

  // Create state
  const [newName, setNewName] = useState('')
  const [newTemplate, setNewTemplate] = useState('basic')
  const [newSymbols, setNewSymbols] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [creating, setCreating] = useState(false)

  // ── Data loading ──
  const loadData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [strats, tmpls] = await Promise.all([
        fetchJSON('/api/workshop/strategies'),
        fetchJSON('/api/workshop/templates'),
      ])
      setStrategies(strats)
      setTemplates(tmpls)
    } catch (e: any) {
      setError(e.message)
    }
    setLoading(false)
  }, [])

  useEffect(() => { loadData() }, [loadData])

  // ── Open strategy in editor ──
  const openStrategy = useCallback(async (sid: string) => {
    setLoading(true)
    setError(null)
    try {
      const [codeRes, configRes] = await Promise.allSettled([
        fetchJSON(`/api/workshop/code/${sid}`),
        fetchJSON(`/api/workshop/config/${sid}`),
      ])
      setActiveStrategy(sid)
      setCode(codeRes.status === 'fulfilled' ? codeRes.value.code : '# Strategy code not found')
      setConfig(configRes.status === 'fulfilled' ? configRes.value.config : '# Config not found')
      setEditorTab('code')
      setValidation(null)
      setDirty(false)
      setView('editor')
    } catch (e: any) {
      setError(e.message)
    }
    setLoading(false)
  }, [])

  // ── Save code/config ──
  const handleSave = useCallback(async () => {
    if (!activeStrategy) return
    setSaving(true)
    setSaveMsg(null)
    try {
      if (editorTab === 'code') {
        await fetchJSON(`/api/workshop/code/${activeStrategy}`, {
          method: 'PUT',
          body: JSON.stringify({ code }),
        })
      } else {
        await fetchJSON(`/api/workshop/config/${activeStrategy}`, {
          method: 'PUT',
          body: JSON.stringify({ config }),
        })
      }
      setSaveMsg('Saved successfully')
      setDirty(false)
      setTimeout(() => setSaveMsg(null), 3000)
    } catch (e: any) {
      const detail = typeof e.message === 'string' ? e.message : JSON.stringify(e.message)
      setSaveMsg(`Error: ${detail}`)
    }
    setSaving(false)
  }, [activeStrategy, editorTab, code, config])

  // ── Validate ──
  const handleValidate = useCallback(async () => {
    setValidating(true)
    try {
      const result = await fetchJSON('/api/workshop/validate', {
        method: 'POST',
        body: JSON.stringify({ code }),
      })
      setValidation(result)
    } catch (e: any) {
      setValidation({ valid: false, errors: [e.message], warnings: [], class_name: null, base_class: null, has_generate_signals: false })
    }
    setValidating(false)
  }, [code])

  // ── Create new strategy ──
  const handleCreate = useCallback(async () => {
    if (!newName.trim()) return
    setCreating(true)
    setError(null)
    try {
      const symbols = newSymbols.split(',').map(s => s.trim()).filter(Boolean)
      const result = await fetchJSON('/api/workshop/scaffold', {
        method: 'POST',
        body: JSON.stringify({
          name: newName,
          template_type: newTemplate,
          symbols: symbols.length > 0 ? symbols : undefined,
          description: newDesc || undefined,
        }),
      })
      // Reload and open the new strategy
      await loadData()
      await openStrategy(result.strategy_id)
    } catch (e: any) {
      setError(e.message)
    }
    setCreating(false)
  }, [newName, newTemplate, newSymbols, newDesc, loadData, openStrategy])

  // ── Keyboard shortcut: Ctrl+S to save ──
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 's' && view === 'editor') {
        e.preventDefault()
        handleSave()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [handleSave, view])

  // ── Render: Strategy List ──
  if (view === 'list') {
    return (
      <div className="space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-2xl font-bold">Strategy Workshop</h2>
            <p className="text-sm text-gray-500 mt-1">Create, edit, and test trading strategies</p>
          </div>
          <button
            onClick={() => setView('create')}
            className="px-4 py-2 bg-accent hover:bg-accent/80 text-white rounded-lg text-sm font-medium transition-colors"
          >
            + New Strategy
          </button>
        </div>

        {error && (
          <div className="bg-danger/10 border border-danger/30 text-danger px-4 py-3 rounded-lg text-sm">
            {error}
          </div>
        )}

        {/* Template Quick Actions */}
        <div className="bg-surface rounded-xl border border-border p-5">
          <h3 className="font-semibold text-gray-200 mb-3">Quick Start Templates</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
            {templates.map((t) => (
              <button
                key={t.name}
                onClick={() => { setNewTemplate(t.name); setView('create') }}
                className="text-left p-4 rounded-lg border border-border hover:border-accent/50 bg-surface-light transition-all group"
              >
                <div className="flex items-center gap-2 mb-2">
                  <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${baseBadge(t.base_class)}`}>
                    {t.base_class.replace('StrategyBase', '').replace('Strategy', '') || 'Basic'}
                  </span>
                </div>
                <h4 className="font-medium text-gray-200 group-hover:text-accent text-sm capitalize transition-colors">
                  {t.name}
                </h4>
                <p className="text-[11px] text-gray-500 mt-1 line-clamp-2">{t.description}</p>
              </button>
            ))}
          </div>
        </div>

        {/* Strategy List */}
        {loading ? (
          <div className="text-center text-gray-500 py-12">Loading strategies...</div>
        ) : (
          <div className="bg-surface rounded-xl border border-border overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border">
                  <th className="text-left text-xs text-gray-500 font-medium px-5 py-3">Strategy</th>
                  <th className="text-left text-xs text-gray-500 font-medium px-5 py-3">Base Class</th>
                  <th className="text-left text-xs text-gray-500 font-medium px-5 py-3">Category</th>
                  <th className="text-right text-xs text-gray-500 font-medium px-5 py-3">Lines</th>
                  <th className="text-center text-xs text-gray-500 font-medium px-5 py-3">Config</th>
                  <th className="text-right text-xs text-gray-500 font-medium px-5 py-3">Actions</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((s) => (
                  <tr key={s.strategy_id} className="border-b border-border/50 hover:bg-surface-light/50 transition-colors">
                    <td className="px-5 py-3">
                      <div className="font-medium text-gray-200 text-sm">{s.strategy_id}</div>
                      <div className="text-[11px] text-gray-500 mt-0.5 line-clamp-1">{s.description}</div>
                    </td>
                    <td className="px-5 py-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${baseBadge(s.base_class)}`}>
                        {s.base_class}
                      </span>
                    </td>
                    <td className="px-5 py-3">
                      <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${assetBadge(s.category)}`}>
                        {s.category}
                      </span>
                    </td>
                    <td className="px-5 py-3 text-right text-sm text-gray-400">{s.lines}</td>
                    <td className="px-5 py-3 text-center">
                      {s.has_config ? (
                        <span className="text-success text-sm">Y</span>
                      ) : (
                        <span className="text-gray-600 text-sm">-</span>
                      )}
                    </td>
                    <td className="px-5 py-3 text-right">
                      <button
                        onClick={() => openStrategy(s.strategy_id)}
                        className="px-3 py-1.5 bg-accent/10 text-accent hover:bg-accent/20 rounded text-xs font-medium transition-colors"
                      >
                        Open
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {strategies.length === 0 && (
              <div className="text-center text-gray-500 py-12">
                No strategies found. Create one to get started!
              </div>
            )}
          </div>
        )}
      </div>
    )
  }

  // ── Render: Create New Strategy ──
  if (view === 'create') {
    const selectedTemplate = templates.find(t => t.name === newTemplate)

    return (
      <div className="space-y-6 max-w-2xl">
        <div className="flex items-center gap-4">
          <button onClick={() => setView('list')} className="text-gray-400 hover:text-gray-100 transition-colors">
            &larr; Back
          </button>
          <h2 className="text-2xl font-bold">New Strategy</h2>
        </div>

        {error && (
          <div className="bg-danger/10 border border-danger/30 text-danger px-4 py-3 rounded-lg text-sm">
            {error}
          </div>
        )}

        <div className="bg-surface rounded-xl border border-border p-6 space-y-5">
          {/* Name */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1.5">Strategy Name</label>
            <input
              type="text"
              value={newName}
              onChange={e => setNewName(e.target.value)}
              placeholder="e.g. BTC Trend Follower"
              className="w-full bg-background border border-border rounded-lg px-4 py-2.5 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent"
            />
          </div>

          {/* Template */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1.5">Template</label>
            <div className="grid grid-cols-2 gap-3">
              {templates.map(t => (
                <button
                  key={t.name}
                  onClick={() => setNewTemplate(t.name)}
                  className={`p-3 rounded-lg border text-left transition-all ${
                    newTemplate === t.name
                      ? 'border-accent bg-accent/10'
                      : 'border-border hover:border-accent/30 bg-background'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${baseBadge(t.base_class)}`}>
                      {t.base_class.replace('StrategyBase', '').replace('Strategy', '') || 'Basic'}
                    </span>
                    <span className="text-sm font-medium text-gray-200 capitalize">{t.name}</span>
                  </div>
                  <p className="text-[11px] text-gray-500 line-clamp-2">{t.description}</p>
                </button>
              ))}
            </div>
          </div>

          {/* Symbols */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1.5">
              Symbols <span className="text-gray-600 font-normal">(comma-separated)</span>
            </label>
            <input
              type="text"
              value={newSymbols}
              onChange={e => setNewSymbols(e.target.value)}
              placeholder={selectedTemplate?.default_symbols.join(', ') || 'BTC/USDT:USDT'}
              className="w-full bg-background border border-border rounded-lg px-4 py-2.5 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent"
            />
          </div>

          {/* Description */}
          <div>
            <label className="block text-sm font-medium text-gray-300 mb-1.5">Description (optional)</label>
            <textarea
              value={newDesc}
              onChange={e => setNewDesc(e.target.value)}
              placeholder="Brief description of the strategy's approach..."
              rows={2}
              className="w-full bg-background border border-border rounded-lg px-4 py-2.5 text-sm text-gray-100 placeholder-gray-600 focus:outline-none focus:border-accent resize-none"
            />
          </div>

          {/* Default Params Preview */}
          {selectedTemplate && (
            <div>
              <label className="block text-sm font-medium text-gray-300 mb-1.5">Default Parameters</label>
              <div className="bg-background rounded-lg border border-border p-3 max-h-40 overflow-auto">
                <pre className="text-xs text-gray-400 font-mono">
                  {Object.entries(selectedTemplate.default_params).map(([k, v]) =>
                    `${k}: ${JSON.stringify(v)}`
                  ).join('\n')}
                </pre>
              </div>
            </div>
          )}

          {/* Create Button */}
          <button
            onClick={handleCreate}
            disabled={creating || !newName.trim()}
            className="w-full py-3 bg-accent hover:bg-accent/80 disabled:bg-accent/30 disabled:cursor-not-allowed text-white rounded-lg font-medium transition-colors"
          >
            {creating ? 'Creating...' : 'Create Strategy'}
          </button>
        </div>
      </div>
    )
  }

  // ── Render: Code Editor ──
  return (
    <div className="flex flex-col h-[calc(100vh-3rem)]">
      {/* Editor Header */}
      <div className="flex items-center justify-between px-1 pb-3 flex-shrink-0">
        <div className="flex items-center gap-4">
          <button onClick={() => { setView('list'); loadData() }} className="text-gray-400 hover:text-gray-100 transition-colors">
            &larr; Back
          </button>
          <div>
            <h2 className="text-lg font-bold">
              {activeStrategy}
              {dirty && <span className="ml-2 text-warning text-sm font-normal">modified</span>}
            </h2>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {/* Validate button */}
          {editorTab === 'code' && (
            <button
              onClick={handleValidate}
              disabled={validating}
              className="px-3 py-1.5 bg-surface border border-border hover:border-accent/50 text-gray-300 rounded-lg text-xs font-medium transition-colors"
            >
              {validating ? 'Checking...' : 'Validate'}
            </button>
          )}

          {/* Save button */}
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-1.5 bg-accent hover:bg-accent/80 disabled:bg-accent/30 text-white rounded-lg text-xs font-medium transition-colors"
          >
            {saving ? 'Saving...' : 'Save'}
          </button>

          {/* Save message */}
          {saveMsg && (
            <span className={`text-xs ${saveMsg.startsWith('Error') ? 'text-danger' : 'text-success'}`}>
              {saveMsg}
            </span>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 pb-2 flex-shrink-0">
        <button
          onClick={() => setEditorTab('code')}
          className={`px-4 py-2 rounded-t-lg text-sm font-medium transition-colors ${
            editorTab === 'code'
              ? 'bg-surface text-gray-100 border border-b-0 border-border'
              : 'text-gray-500 hover:text-gray-300'
          }`}
        >
          Strategy Code (.py)
        </button>
        <button
          onClick={() => setEditorTab('config')}
          className={`px-4 py-2 rounded-t-lg text-sm font-medium transition-colors ${
            editorTab === 'config'
              ? 'bg-surface text-gray-100 border border-b-0 border-border'
              : 'text-gray-500 hover:text-gray-300'
          }`}
        >
          Config (.yaml)
        </button>
      </div>

      {/* Editor + Validation Panel */}
      <div className="flex-1 flex gap-0 min-h-0">
        {/* Monaco Editor */}
        <div className="flex-1 border border-border rounded-lg overflow-hidden">
          <MonacoEditor
            height="100%"
            language={editorTab === 'code' ? 'python' : 'yaml'}
            theme="vs-dark"
            value={editorTab === 'code' ? code : config}
            onChange={(val) => {
              if (editorTab === 'code') {
                setCode(val || '')
              } else {
                setConfig(val || '')
              }
              setDirty(true)
            }}
            options={{
              fontSize: 13,
              fontFamily: "'JetBrains Mono', 'Fira Code', Consolas, monospace",
              minimap: { enabled: true },
              scrollBeyondLastLine: false,
              wordWrap: 'off',
              tabSize: 4,
              insertSpaces: true,
              renderLineHighlight: 'all',
              bracketPairColorization: { enabled: true },
              padding: { top: 8 },
            }}
          />
        </div>

        {/* Validation Sidebar */}
        {validation && editorTab === 'code' && (
          <div className="w-72 ml-3 bg-surface border border-border rounded-lg p-4 overflow-auto flex-shrink-0">
            <h3 className="font-semibold text-gray-200 text-sm mb-3">Validation</h3>

            {/* Status */}
            <div className={`flex items-center gap-2 mb-3 px-3 py-2 rounded-lg ${
              validation.valid ? 'bg-success/10' : 'bg-danger/10'
            }`}>
              <span className={`w-2 h-2 rounded-full ${validation.valid ? 'bg-success' : 'bg-danger'}`} />
              <span className={`text-sm font-medium ${validation.valid ? 'text-success' : 'text-danger'}`}>
                {validation.valid ? 'Valid Python' : 'Syntax Error'}
              </span>
            </div>

            {/* Class info */}
            {validation.class_name && (
              <div className="mb-3 space-y-1">
                <div className="text-xs text-gray-500">Class</div>
                <div className="text-sm text-gray-200">{validation.class_name}</div>
                <div className="text-xs text-gray-500 mt-1">Base</div>
                <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${baseBadge(validation.base_class || '')}`}>
                  {validation.base_class}
                </span>
                <div className="text-xs text-gray-500 mt-1">generate_signals()</div>
                <span className={`text-xs ${validation.has_generate_signals ? 'text-success' : 'text-warning'}`}>
                  {validation.has_generate_signals ? 'Defined' : 'Missing'}
                </span>
              </div>
            )}

            {/* Errors */}
            {validation.errors.length > 0 && (
              <div className="mb-3">
                <div className="text-xs text-danger font-medium mb-1">Errors</div>
                {validation.errors.map((e, i) => (
                  <div key={i} className="text-xs text-danger/80 bg-danger/5 rounded px-2 py-1.5 mb-1 font-mono">
                    {e}
                  </div>
                ))}
              </div>
            )}

            {/* Warnings */}
            {validation.warnings.length > 0 && (
              <div>
                <div className="text-xs text-warning font-medium mb-1">Warnings</div>
                {validation.warnings.map((w, i) => (
                  <div key={i} className="text-xs text-warning/80 bg-warning/5 rounded px-2 py-1.5 mb-1">
                    {w}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
