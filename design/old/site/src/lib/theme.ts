import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'
const KEY = 'sr-site-theme'
const query = '(prefers-color-scheme: dark)'

function readPref(): Theme | null {
  try {
    const v = localStorage.getItem(KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    return null
  }
}

/** The site theme. `pref` is null while the site follows the system setting. */
export function useTheme() {
  const [pref, setPref] = useState<Theme | null>(readPref)
  const [system, setSystem] = useState<Theme>(() => (window.matchMedia(query).matches ? 'dark' : 'light'))

  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = () => setSystem(mq.matches ? 'dark' : 'light')
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [])

  const theme = pref ?? system
  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark')
    document.documentElement.style.colorScheme = theme
  }, [theme])

  const toggle = () => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    const p = next === system ? null : next
    setPref(p)
    try {
      if (p) localStorage.setItem(KEY, p)
      else localStorage.removeItem(KEY)
    } catch {
      /* storage can be off for file:// pages */
    }
  }

  return { theme, pref, toggle }
}

/** Forces a theme on an embedded page by rewriting its color-scheme media queries. */
export function themedHtml(html: string, pref: Theme | null): string {
  if (!pref) return html
  const off: Theme = pref === 'dark' ? 'light' : 'dark'
  return html
    .split(`(prefers-color-scheme: ${pref})`)
    .join('all')
    .split(`(prefers-color-scheme: ${off})`)
    .join('not all')
    .replace('color-scheme: light dark', `color-scheme: ${pref}`)
}
