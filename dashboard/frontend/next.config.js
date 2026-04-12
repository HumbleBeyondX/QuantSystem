/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export for Electron desktop packaging
  output: 'export',
  // Disable image optimization (not available in static export)
  images: {
    unoptimized: true,
  },
  // Trailing slashes for file-based routing in Electron
  trailingSlash: true,
  // No assetPrefix — Electron custom 'app://' protocol handles path resolution
  // from any subdirectory (workshop/, strategies/xxx/, etc.)
}

module.exports = nextConfig
