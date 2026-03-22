/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Dark theme colors matching reference design
        background: '#0a0e17',
        surface: '#111827',
        'surface-light': '#1a2332',
        border: '#1f2937',
        primary: '#3b82f6',
        'primary-light': '#60a5fa',
        success: '#10b981',
        danger: '#ef4444',
        warning: '#f59e0b',
        accent: '#8b5cf6',
      },
    },
  },
  plugins: [],
}
