'use client'

import { createContext, useContext, ReactNode } from 'react'

interface ThemeContextValue {
  mode: 'dark'
  colors: {
    background: string
    surface: string
    border: string
    accent: string
    success: string
    danger: string
    warning: string
  }
}

const THEME: ThemeContextValue = {
  mode: 'dark',
  colors: {
    background: '#0a0e17',
    surface: '#111827',
    border: '#1f2937',
    accent: '#3b82f6',
    success: '#10b981',
    danger: '#ef4444',
    warning: '#f59e0b',
  },
}

const ThemeContext = createContext<ThemeContextValue>(THEME)

export function ThemeProvider({ children }: { children: ReactNode }) {
  return (
    <ThemeContext.Provider value={THEME}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  return useContext(ThemeContext)
}
