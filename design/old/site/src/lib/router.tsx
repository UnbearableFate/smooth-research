import { useEffect, useState, type AnchorHTMLAttributes } from 'react'
import { chapterBySlug, isViewKey, type ViewKey } from './content'

export type Route =
  | { name: 'home' }
  | { name: 'doc'; slug: string; section: string }
  | { name: 'view'; view: ViewKey; anchor: string }
  | { name: 'missing'; path: string }

export function parseHash(hash: string): Route {
  let path = hash.replace(/^#/, '')
  try {
    path = decodeURIComponent(path)
  } catch {
    /* keep the raw path */
  }
  const parts = path.split('/').filter(Boolean)
  if (parts.length === 0) return { name: 'home' }
  if (parts[0] === 'doc' && parts[1] && chapterBySlug.has(parts[1])) return { name: 'doc', slug: parts[1], section: parts[2] ?? '' }
  if (parts[0] === 'view' && parts[1] && isViewKey(parts[1])) return { name: 'view', view: parts[1], anchor: parts[2] ?? '' }
  return { name: 'missing', path }
}

export const hrefHome = '#/'
export const hrefDoc = (slug: string, section?: string) => `#/doc/${slug}${section ? `/${section}` : ''}`
export const hrefView = (view: string, anchor?: string) => `#/view/${view}${anchor ? `/${anchor}` : ''}`

let repeat: (() => void) | null = null

function sameHash(a: string, b: string) {
  const norm = (h: string) => (h === '' || h === '#' ? '#/' : h)
  return norm(a) === norm(b)
}

/** Goes to a hash route; following the link of the current route again re-runs its scroll. */
export function navigate(href: string) {
  if (sameHash(window.location.hash, href)) repeat?.()
  else window.location.hash = href
}

export function useRoute() {
  const [state, setState] = useState(() => ({ route: parseHash(window.location.hash), seq: 0 }))
  useEffect(() => {
    const update = () => setState((s) => ({ route: parseHash(window.location.hash), seq: s.seq + 1 }))
    repeat = update
    window.addEventListener('hashchange', update)
    return () => {
      window.removeEventListener('hashchange', update)
      repeat = null
    }
  }, [])
  return state
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }

export function Link({ href, onClick, ...rest }: LinkProps) {
  return (
    <a
      href={href}
      onClick={(e) => {
        onClick?.(e)
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
        if (!href.startsWith('#/')) return
        e.preventDefault()
        navigate(href)
      }}
      {...rest}
    />
  )
}
