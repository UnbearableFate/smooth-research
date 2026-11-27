import { GROUPS, type NavItem } from '@/lib/config'
import { chapterBySlug, chapters } from '@/lib/content'
import { Link, hrefDoc, hrefHome, hrefView, type Route } from '@/lib/router'
import { cn } from '@/lib/utils'

const listed = new Set(GROUPS.flatMap((g) => g.items.flatMap((i) => (i.kind === 'chapter' ? [i.slug] : []))))
const extra = chapters.filter((c) => !listed.has(c.slug))
const groups = extra.length
  ? [...GROUPS, { label: '其他', items: extra.map((c): NavItem => ({ kind: 'chapter', slug: c.slug })) }]
  : GROUPS

function isActive(item: NavItem, route: Route): boolean {
  if (item.kind === 'home') return route.name === 'home'
  if (item.kind === 'chapter') return route.name === 'doc' && route.slug === item.slug
  if (route.name !== 'view' || route.view !== item.view) return false
  const pinned = groups.some((g) => g.items.some((i) => i.kind === 'view' && i.view === item.view && i.anchor && i.anchor === route.anchor))
  return item.anchor ? item.anchor === route.anchor : !pinned
}

export function Nav({ route, onNavigate }: { route: Route; onNavigate?: () => void }) {
  return (
    <nav aria-label="目录" className="space-y-6 px-3 py-6 text-[0.875rem]">
      {groups.map((g) => (
        <div key={g.label}>
          <p className="mb-1.5 px-3 text-xs font-medium tracking-wide text-muted-foreground">{g.label}</p>
          <ul className="space-y-px">
            {g.items.map((item) => {
              const active = isActive(item, route)
              let href = hrefHome
              let num = ''
              let label = '首页'
              let note = ''
              if (item.kind === 'chapter') {
                const c = chapterBySlug.get(item.slug)
                if (!c) return null
                href = hrefDoc(c.slug)
                num = c.num
                label = c.title
              } else if (item.kind === 'view') {
                href = hrefView(item.view, item.anchor)
                label = item.label
                note = item.note ?? ''
              }
              return (
                <li key={href}>
                  <Link
                    href={href}
                    onClick={onNavigate}
                    aria-current={active ? 'page' : undefined}
                    className={cn(
                      'flex items-baseline gap-2 rounded-sm border-l-2 py-1 pl-[10px] pr-2 leading-snug transition-colors',
                      active
                        ? 'border-brand bg-accent font-medium text-foreground'
                        : 'border-transparent text-muted-foreground hover:bg-accent/60 hover:text-foreground',
                    )}
                  >
                    <span className={cn('w-5 shrink-0 text-right font-mono text-xs', active ? 'text-brand' : 'opacity-70')}>
                      {item.kind === 'view' ? '◇' : num}
                    </span>
                    <span className="min-w-0">
                      {label}
                      {note ? <span className="ml-1.5 text-xs text-muted-foreground">（{note}）</span> : null}
                    </span>
                  </Link>
                </li>
              )
            })}
          </ul>
        </div>
      ))}
    </nav>
  )
}
