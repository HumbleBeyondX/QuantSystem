'use client'

interface CorrelationHeatmapProps {
  labels: string[]
  matrix: number[][]
}

export default function CorrelationHeatmap({
  labels,
  matrix,
}: CorrelationHeatmapProps) {
  if (labels.length === 0) {
    return (
      <div className="flex items-center justify-center text-gray-500 text-sm h-48">
        No correlation data
      </div>
    )
  }

  return (
    <div className="overflow-x-auto">
      <table className="text-xs">
        <thead>
          <tr>
            <th className="p-1" />
            {labels.map((label) => (
              <th key={label} className="p-1 text-gray-400 font-normal">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {matrix.map((row, i) => (
            <tr key={labels[i]}>
              <td className="p-1 text-gray-400 pr-2">{labels[i]}</td>
              {row.map((val, j) => (
                <td
                  key={j}
                  className="p-1 text-center rounded"
                  style={{ backgroundColor: correlationColor(val) }}
                >
                  {val.toFixed(2)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function correlationColor(value: number): string {
  // Red for negative, blue for positive, grey for zero
  if (value > 0) {
    const intensity = Math.min(value, 1)
    return `rgba(59, 130, 246, ${intensity * 0.5})`
  } else {
    const intensity = Math.min(Math.abs(value), 1)
    return `rgba(239, 68, 68, ${intensity * 0.5})`
  }
}
