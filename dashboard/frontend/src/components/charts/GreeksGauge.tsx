'use client'

interface GreeksGaugeProps {
  label: string
  value: number
  min?: number
  max?: number
  unit?: string
}

export default function GreeksGauge({
  label,
  value,
  min = -1,
  max = 1,
  unit = '',
}: GreeksGaugeProps) {
  const range = max - min
  const pct = Math.max(0, Math.min(100, ((value - min) / range) * 100))
  const isNeutral = Math.abs(value) < range * 0.1

  const barColor = isNeutral
    ? 'bg-success'
    : Math.abs(value) > range * 0.4
      ? 'bg-danger'
      : 'bg-warning'

  return (
    <div className="bg-surface rounded-lg border border-border p-3">
      <div className="flex justify-between items-center mb-2">
        <span className="text-xs text-gray-400">{label}</span>
        <span className="text-sm font-mono font-medium text-gray-100">
          {value >= 0 ? '+' : ''}
          {value.toFixed(4)}
          {unit}
        </span>
      </div>
      <div className="relative h-2 bg-gray-800 rounded-full overflow-hidden">
        {/* Center marker */}
        <div
          className="absolute top-0 bottom-0 w-px bg-gray-500 z-10"
          style={{ left: `${((0 - min) / range) * 100}%` }}
        />
        {/* Value bar */}
        <div
          className={`absolute top-0 bottom-0 ${barColor} rounded-full transition-all duration-300`}
          style={{
            left: `${Math.min(pct, ((0 - min) / range) * 100)}%`,
            width: `${Math.abs(pct - ((0 - min) / range) * 100)}%`,
          }}
        />
      </div>
      <div className="flex justify-between mt-1">
        <span className="text-[10px] text-gray-600">{min}</span>
        <span className="text-[10px] text-gray-600">{max}</span>
      </div>
    </div>
  )
}
