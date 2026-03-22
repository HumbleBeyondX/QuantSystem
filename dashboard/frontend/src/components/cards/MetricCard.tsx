'use client'

interface MetricCardProps {
  title: string
  value: string | number
  subtitle?: string
  change?: number
  color?: string
  size?: 'sm' | 'md' | 'lg'
}

export default function MetricCard({
  title,
  value,
  subtitle,
  change,
  color,
  size = 'md',
}: MetricCardProps) {
  const textSize = {
    sm: 'text-lg',
    md: 'text-2xl',
    lg: 'text-3xl',
  }[size]

  const resolvedColor =
    color ??
    (typeof change === 'number'
      ? change >= 0
        ? 'text-success'
        : 'text-danger'
      : 'text-gray-100')

  return (
    <div className="bg-surface rounded-xl border border-border p-4 hover:border-accent/30 transition-colors">
      <p className="text-sm text-gray-400">{title}</p>
      <p className={`${textSize} font-bold mt-1 ${resolvedColor}`}>
        {typeof value === 'number' ? formatValue(value) : value}
      </p>
      <div className="flex items-center gap-2 mt-1">
        {typeof change === 'number' && (
          <span
            className={`text-xs font-medium ${change >= 0 ? 'text-success' : 'text-danger'}`}
          >
            {change >= 0 ? '+' : ''}
            {change.toFixed(2)}%
          </span>
        )}
        {subtitle && <span className="text-xs text-gray-500">{subtitle}</span>}
      </div>
    </div>
  )
}

function formatValue(val: number): string {
  if (Math.abs(val) >= 1_000_000) return `$${(val / 1_000_000).toFixed(2)}M`
  if (Math.abs(val) >= 1_000) return `$${(val / 1_000).toFixed(1)}K`
  return val.toLocaleString()
}
