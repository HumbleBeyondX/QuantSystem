'use client'

interface Performer {
  symbol: string
  pnl: number
  pnl_pct: number
  quantity: number
}

interface TopPerformersProps {
  performers: Performer[]
  title?: string
}

export default function TopPerformers({
  performers,
  title = 'Top Performers',
}: TopPerformersProps) {
  return (
    <div className="bg-surface rounded-xl border border-border p-4">
      <h3 className="text-sm font-semibold text-gray-300 mb-3">{title}</h3>
      {performers.length === 0 ? (
        <p className="text-xs text-gray-500">No positions</p>
      ) : (
        <table className="w-full text-xs">
          <thead>
            <tr className="text-gray-500 border-b border-border">
              <th className="text-left pb-2 font-normal">Symbol</th>
              <th className="text-right pb-2 font-normal">Qty</th>
              <th className="text-right pb-2 font-normal">P&L</th>
              <th className="text-right pb-2 font-normal">%</th>
            </tr>
          </thead>
          <tbody>
            {performers.map((p) => (
              <tr key={p.symbol} className="border-b border-border/50 last:border-0">
                <td className="py-1.5 font-medium text-gray-200">{p.symbol}</td>
                <td className="py-1.5 text-right text-gray-400">{p.quantity}</td>
                <td
                  className={`py-1.5 text-right font-medium ${
                    p.pnl >= 0 ? 'text-success' : 'text-danger'
                  }`}
                >
                  {p.pnl >= 0 ? '+' : ''}${p.pnl.toLocaleString()}
                </td>
                <td
                  className={`py-1.5 text-right ${
                    p.pnl_pct >= 0 ? 'text-success' : 'text-danger'
                  }`}
                >
                  {p.pnl_pct >= 0 ? '+' : ''}
                  {p.pnl_pct.toFixed(2)}%
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
