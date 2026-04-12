// Server component that provides static params for the dynamic route
// Required for `output: 'export'` in next.config.js

export function generateStaticParams() {
  return [
    { id: 'momentum-btc' },
    { id: 'mean-rev-eth' },
    { id: 'pairs-tech' },
    { id: 'spy-gamma-scalp' },
    { id: 'vol-arb' },
    { id: 'sector-rotation' },
    { id: 'crypto-macro-cycle' },
  ]
}

export default function StrategyDetailLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <>{children}</>
}
