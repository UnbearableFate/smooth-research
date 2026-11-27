import { useEffect, useState } from 'react'
import { Menu, Moon, Search, Sun } from 'lucide-react'
import { Sheet, SheetContent, SheetTitle } from '@/components/ui/sheet'
import { DocPage } from '@/components/DocPage'
import { HomePage } from '@/components/HomePage'
import { Nav } from '@/components/Nav'
import { SearchDialog } from '@/components/SearchDialog'
import { ViewPage } from '@/components/ViewPage'
import { VIEW_LABEL } from '@/lib/config'
import { chapterBySlug, chapterName } from '@/lib/content'
import { Link, hrefHome, useRoute, type Route } from '@/lib/router'
import { useTheme } from '@/lib/theme'

const SITE = 'Smooth Research 新设计'
const isMac = /Mac|iPhone|iPad/.test(navigator.userAgent)

function titleFor(route: Route): string {
  if (route.name === 'doc') {
    const c = chapterBySlug.get(route.slug)
    return c ? `${chapterName(c)} · ${SITE}` : SITE
  }
  if (route.name === 'view') return `${VIEW_LABEL[route.view]} · 图解 · ${SITE}`
  return SITE
}

export function Mark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <rect width="32" height="32" rx="4" fill="hsl(var(--brand))" />
      <path d="M16 11v5M16 16l-6 6M16 16l6 6" stroke="#fff" strokeWidth="2.2" fill="none" />
      <circle cx="16" cy="9.5" r="3" fill="#fff" />
      <circle cx="9.5" cy="22.5" r="3" fill="#fff" />
      <circle cx="22.5" cy="22.5" r="3" fill="#fff" />
    </svg>
  )
}

export default function App() {
  const { route, seq } = useRoute()
  const { theme, pref, toggle } = useTheme()
  const [menu, setMenu] = useState(false)
  const [search, setSearch] = useState(false)

  useEffect(() => {
    document.title = titleFor(route)
    if (route.name === 'view') window.scrollTo(0, 0)
  }, [route, seq])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.key === 'k' || e.key === 'K') && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        setSearch((o) => !o)
        return
      }
      if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey) {
        const t = e.target as HTMLElement | null
        if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return
        e.preventDefault()
        setSearch(true)
      }
    }
    const onMessage = (e: MessageEvent) => {
      if ((e.data as { type?: string } | null)?.type === 'sr-search') setSearch(true)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('message', onMessage)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('message', onMessage)
    }
  }, [])

  let page
  if (route.name === 'home') page = <HomePage seq={seq} />
  else if (route.name === 'doc') page = <DocPage key={route.slug} slug={route.slug} section={route.section} seq={seq} />
  else if (route.name === 'view') page = <ViewPage viewKey={route.view} anchor={route.anchor} seq={seq} pref={pref} />
  else
    page = (
      <div className="px-5 py-16 sm:px-8 lg:px-12">
        <p className="text-muted-foreground">没有这一页：{route.path}</p>
        <Link href={hrefHome} className="mt-3 inline-block text-brand underline underline-offset-4">
          回到首页
        </Link>
      </div>
    )

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-40 flex h-12 items-center gap-2 border-b bg-background px-3 lg:px-4">
        <button
          type="button"
          onClick={() => setMenu(true)}
          aria-label="打开目录"
          className="-ml-1 grid h-9 w-9 place-items-center rounded text-muted-foreground hover:bg-accent hover:text-foreground lg:hidden"
        >
          <Menu className="h-5 w-5" />
        </button>
        <Link href={hrefHome} className="flex min-w-0 items-center gap-2.5">
          <Mark className="h-6 w-6 shrink-0" />
          <span className="truncate font-semibold tracking-tight">Smooth Research</span>
          <span className="hidden text-muted-foreground sm:inline">新设计</span>
        </Link>
        <span className="hidden rounded-sm border px-1.5 text-xs leading-5 text-muted-foreground sm:inline">草案</span>
        <div className="ml-auto flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => setSearch(true)}
            aria-label="搜索"
            className="flex h-8 items-center gap-2 rounded border bg-card px-2.5 text-sm text-muted-foreground hover:border-brand hover:text-foreground sm:w-64"
          >
            <Search className="h-4 w-4 shrink-0" />
            <span className="hidden sm:inline">搜索文档和图解</span>
            <kbd className="ml-auto hidden rounded-sm border px-1 font-mono text-[0.7rem] leading-4 sm:inline">
              {isMac ? '⌘' : 'Ctrl'} K
            </kbd>
          </button>
          <button
            type="button"
            onClick={toggle}
            aria-label={theme === 'dark' ? '切换到浅色' : '切换到深色'}
            title={theme === 'dark' ? '切换到浅色' : '切换到深色'}
            className="grid h-8 w-8 place-items-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </button>
        </div>
      </header>

      <div className="flex">
        <aside className="sticky top-12 hidden h-[calc(100vh-3rem)] w-64 shrink-0 overflow-y-auto border-r lg:block">
          <Nav route={route} />
        </aside>
        <main className="min-w-0 flex-1">{page}</main>
      </div>

      <Sheet open={menu} onOpenChange={setMenu}>
        <SheetContent side="left" aria-describedby={undefined} className="w-[18rem] overflow-y-auto p-0 sm:max-w-[18rem]">
          <SheetTitle className="flex items-center gap-2 border-b px-5 py-3 text-base">
            <Mark className="h-5 w-5" />
            目录
          </SheetTitle>
          <Nav route={route} onNavigate={() => setMenu(false)} />
        </SheetContent>
      </Sheet>
      <SearchDialog open={search} onOpenChange={setSearch} />
    </div>
  )
}
