'use client'

import { useEffect, useState } from 'react'

interface HeaderProps {
  title?: string
}

export default function Header({ title }: HeaderProps) {
  const [clock, setClock] = useState('')

  useEffect(() => {
    const tick = () => {
      const now = new Date()
      setClock(
        now.toLocaleTimeString('en-US', {
          hour12: false,
          hour: '2-digit',
          minute: '2-digit',
          second: '2-digit',
        }) + ' UTC'
      )
    }
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [])

  return (
    <header className="h-14 border-b border-border bg-surface flex items-center justify-between px-6">
      <div>
        {title && <h2 className="text-lg font-semibold text-gray-100">{title}</h2>}
      </div>

      <div className="flex items-center gap-6">
        {/* Market status indicators */}
        <div className="flex items-center gap-4 text-xs text-gray-400">
          <span className="flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-success" />
            Crypto 24/7
          </span>
          <span className="flex items-center gap-1.5">
            <MarketStatusDot />
            US Equities
          </span>
        </div>

        {/* Clock */}
        <span className="text-xs text-gray-500 font-mono">{clock}</span>
      </div>
    </header>
  )
}

function MarketStatusDot() {
  const now = new Date()
  const utcHour = now.getUTCHours()
  const utcDay = now.getUTCDay()
  // NYSE: Mon-Fri 14:30-21:00 UTC
  const isOpen =
    utcDay >= 1 &&
    utcDay <= 5 &&
    ((utcHour === 14 && now.getUTCMinutes() >= 30) ||
      (utcHour > 14 && utcHour < 21))

  return (
    <span
      className={`w-1.5 h-1.5 rounded-full ${isOpen ? 'bg-success' : 'bg-gray-600'}`}
    />
  )
}
