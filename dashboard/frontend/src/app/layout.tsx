import type { Metadata } from 'next'
import Link from 'next/link'
import './globals.css'

export const metadata: Metadata = {
  title: 'QuantSystem Dashboard',
  description: 'Production Quantitative Trading System',
}

const navItems = [
  { href: '/', label: 'Overview', icon: '◎' },
  { href: '/portfolio', label: 'Portfolio', icon: '◈' },
  { href: '/strategies', label: 'Strategies', icon: '⚡' },
  { href: '/trading', label: 'Trading', icon: '⚙' },
  { href: '/backtest', label: 'Backtest', icon: '↻' },
  { href: '/risk', label: 'Risk', icon: '⛊' },
  { href: '/market', label: 'Market Data', icon: '▥' },
]

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en" className="dark">
      <body className="bg-background text-gray-100 min-h-screen">
        <div className="flex h-screen">
          {/* Sidebar */}
          <aside className="w-60 bg-surface border-r border-border flex-shrink-0 flex flex-col">
            <div className="p-5 border-b border-border">
              <h1 className="text-lg font-bold text-primary tracking-tight">QuantSystem</h1>
              <p className="text-[10px] text-gray-500 mt-0.5 tracking-wider">QUANTITATIVE TRADING v0.1</p>
            </div>
            <nav className="flex-1 px-3 py-4 space-y-0.5">
              {navItems.map(item => (
                <Link
                  key={item.href}
                  href={item.href}
                  className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm text-gray-400 hover:text-gray-100 hover:bg-surface-light transition-colors"
                >
                  <span className="text-base w-5 text-center opacity-60">{item.icon}</span>
                  {item.label}
                </Link>
              ))}
            </nav>
            <div className="p-4 border-t border-border">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 rounded-full bg-success animate-pulse" />
                <span className="text-xs text-gray-500">System Online</span>
              </div>
            </div>
          </aside>

          {/* Main content */}
          <main className="flex-1 overflow-auto p-6">
            {children}
          </main>
        </div>
      </body>
    </html>
  )
}
