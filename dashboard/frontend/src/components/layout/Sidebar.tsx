'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'

interface NavItem {
  label: string
  href: string
  icon: string
}

const NAV_ITEMS: NavItem[] = [
  { label: 'Overview', href: '/', icon: '◈' },
  { label: 'Portfolio', href: '/portfolio', icon: '◫' },
  { label: 'Strategies', href: '/strategies', icon: '⚙' },
  { label: 'Backtest', href: '/backtest', icon: '▶' },
  { label: 'Risk', href: '/risk', icon: '⛊' },
  { label: 'Market Data', href: '/market', icon: '◰' },
]

export default function Sidebar() {
  const pathname = usePathname()

  return (
    <aside className="w-56 bg-surface border-r border-border flex flex-col h-screen sticky top-0">
      {/* Logo */}
      <div className="p-4 border-b border-border">
        <h1 className="text-lg font-bold text-accent tracking-wide">QuantSystem</h1>
        <p className="text-xs text-gray-500 mt-0.5">Trading Dashboard</p>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-4">
        <ul className="space-y-1 px-2">
          {NAV_ITEMS.map((item) => {
            const isActive =
              item.href === '/'
                ? pathname === '/'
                : pathname.startsWith(item.href)

            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  className={`
                    flex items-center gap-3 px-3 py-2 rounded-lg text-sm
                    transition-colors duration-150
                    ${
                      isActive
                        ? 'bg-accent/10 text-accent font-medium'
                        : 'text-gray-400 hover:text-gray-200 hover:bg-white/5'
                    }
                  `}
                >
                  <span className="text-base">{item.icon}</span>
                  <span>{item.label}</span>
                </Link>
              </li>
            )
          })}
        </ul>
      </nav>

      {/* Status footer */}
      <div className="p-4 border-t border-border">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-success animate-pulse" />
          <span className="text-xs text-gray-500">System Online</span>
        </div>
      </div>
    </aside>
  )
}
