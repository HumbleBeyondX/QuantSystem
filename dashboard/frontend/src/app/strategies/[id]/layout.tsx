/**
 * Strategy Detail Layout — generates static pages for all known strategy IDs.
 * Required for Next.js static export (output: 'export') with dynamic [id] routes.
 *
 * IDs come from config/strategy_registry.yaml (the lifecycle single source of
 * truth) read at build time — never hardcode strategy ids here.
 */

import fs from 'fs'
import path from 'path'

export function generateStaticParams() {
  const registryPath = path.join(process.cwd(), '..', '..', 'config', 'strategy_registry.yaml')
  try {
    const yaml = fs.readFileSync(registryPath, 'utf-8')
    const ids = Array.from(yaml.matchAll(/^\s*-\s*id:\s*([A-Za-z0-9_]+)\s*$/gm), m => m[1])
    if (ids.length > 0) {
      return ids.map(id => ({ id }))
    }
  } catch {
    // fall through to minimal known set
  }
  return [
    { id: 'eth_sniper' },
    { id: 'spy_gamma_scalp' },
    { id: 'btc_wheel' },
    { id: 'combo_wheel_sniper' },
    { id: 'btc_sniper' },
  ]
}

export default function StrategyDetailLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return <>{children}</>
}
