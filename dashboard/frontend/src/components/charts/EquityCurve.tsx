'use client'

import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'

interface DataPoint {
  timestamp: string
  equity: number
  drawdown?: number
}

interface EquityCurveProps {
  data: DataPoint[]
  height?: number
  showDrawdown?: boolean
}

export default function EquityCurve({
  data,
  height = 300,
  showDrawdown = false,
}: EquityCurveProps) {
  if (data.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-gray-500 text-sm"
        style={{ height }}
      >
        No equity data available
      </div>
    )
  }

  const startEquity = data[0]?.equity ?? 0

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis
          dataKey="timestamp"
          tick={{ fill: '#6b7280', fontSize: 11 }}
          tickFormatter={(v: string) => v.slice(5, 10)}
          stroke="#1f2937"
        />
        <YAxis
          tick={{ fill: '#6b7280', fontSize: 11 }}
          stroke="#1f2937"
          tickFormatter={(v: number) => `$${(v / 1000).toFixed(0)}k`}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: '#111827',
            border: '1px solid #1f2937',
            borderRadius: 8,
            fontSize: 12,
          }}
          labelStyle={{ color: '#9ca3af' }}
          formatter={(value: number) => [`$${value.toLocaleString()}`, 'Equity']}
        />
        <ReferenceLine y={startEquity} stroke="#6b7280" strokeDasharray="3 3" />
        <Line
          type="monotone"
          dataKey="equity"
          stroke="#3b82f6"
          strokeWidth={2}
          dot={false}
          activeDot={{ r: 4, fill: '#3b82f6' }}
        />
        {showDrawdown && (
          <Line
            type="monotone"
            dataKey="drawdown"
            stroke="#ef4444"
            strokeWidth={1}
            dot={false}
            yAxisId="right"
          />
        )}
      </LineChart>
    </ResponsiveContainer>
  )
}
