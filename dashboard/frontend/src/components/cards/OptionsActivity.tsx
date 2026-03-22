'use client'

interface OptionsFlow {
  symbol: string
  type: 'call' | 'put'
  strike: number
  expiry: string
  premium: number
  volume: number
  oi: number
  sentiment: 'bullish' | 'bearish' | 'neutral'
  timestamp: string
}

interface OptionsActivityProps {
  flows: OptionsFlow[]
  title?: string
  maxItems?: number
}

export default function OptionsActivity({
  flows,
  title = 'Unusual Options Activity',
  maxItems = 10,
}: OptionsActivityProps) {
  const display = flows.slice(0, maxItems)

  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">{title}</h3>
      {display.length === 0 ? (
        <p className="text-xs text-gray-500">No activity</p>
      ) : (
        <div className="space-y-2 max-h-64 overflow-y-auto">
          {display.map((flow, i) => (
            <div
              key={i}
              className="flex items-center justify-between py-1.5 border-b border-border/50 last:border-0"
            >
              <div className="flex items-center gap-2">
                <span
                  className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                    flow.type === 'call'
                      ? 'bg-success/20 text-success'
                      : 'bg-danger/20 text-danger'
                  }`}
                >
                  {flow.type.toUpperCase()}
                </span>
                <span className="text-xs text-gray-200 font-medium">
                  {flow.symbol}
                </span>
                <span className="text-[10px] text-gray-500">
                  ${flow.strike} {flow.expiry}
                </span>
              </div>
              <div className="flex items-center gap-3">
                <span className="text-xs text-gray-400">
                  Vol: {flow.volume.toLocaleString()}
                </span>
                <span className="text-xs font-medium text-gray-200">
                  ${(flow.premium / 1000).toFixed(1)}K
                </span>
                <SentimentBadge sentiment={flow.sentiment} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function SentimentBadge({ sentiment }: { sentiment: string }) {
  const color = {
    bullish: 'text-success',
    bearish: 'text-danger',
    neutral: 'text-gray-400',
  }[sentiment] ?? 'text-gray-400'

  return <span className={`text-[10px] font-medium ${color}`}>{sentiment}</span>
}
