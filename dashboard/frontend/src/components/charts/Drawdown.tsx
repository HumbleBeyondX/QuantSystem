'use client'

import {
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from 'recharts'

interface DrawdownPoint {
  timestamp: string
  drawdown: number
}

interface DrawdownChartProps {
  data: DrawdownPoint[]
  height?: number
}

export default function DrawdownChart({ data, height = 200 }: DrawdownChartProps) {
  if (data.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-gray-500 text-sm"
        style={{ height }}
      >
        No drawdown data
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
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
          tickFormatter={(v: number) => `${(v * 100).toFixed(1)}%`}
        />
        <Tooltip
          contentStyle={{
            backgroundColor: '#111827',
            border: '1px solid #1f2937',
            borderRadius: 8,
            fontSize: 12,
          }}
          formatter={(value: number) => [`${(value * 100).toFixed(2)}%`, 'Drawdown']}
        />
        <Area
          type="monotone"
          dataKey="drawdown"
          stroke="#ef4444"
          fill="#ef444420"
          strokeWidth={1.5}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}
