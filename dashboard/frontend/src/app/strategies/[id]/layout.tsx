/**
 * Strategy Detail Layout — generates static pages for all known strategy IDs.
 * Required for Next.js static export (output: 'export') with dynamic [id] routes.
 */

export function generateStaticParams() {
  return [
    // Crypto strategies
    { id: 'btc_sniper' },
    { id: 'crypto_scalping' },
    { id: 'crypto_swing' },
    { id: 'crypto_momentum' },
    { id: 'crypto_breakout' },
    { id: 'crypto_orderbook' },
    { id: 'crypto_event' },
    { id: 'crypto_macro_cycle' },
    // Equity strategies
    { id: 'equity_factor' },
    { id: 'equity_timing' },
    { id: 'equity_long_short' },
    // Options strategies
    { id: 'spy_gamma_scalp' },
    { id: 'delta_neutral' },
    { id: 'spy_iron_condor' },
    { id: 'spy_wheel' },
  ]
}

export default function StrategyDetailLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <>{children}</>
}
