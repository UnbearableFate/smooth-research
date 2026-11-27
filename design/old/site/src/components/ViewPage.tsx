import { useCallback, useEffect, useMemo, useRef } from 'react'
import { VIEW_CHAPTERS, VIEW_LABEL, VIEW_NOTE } from '@/lib/config'
import { chapterBySlug, viewByKey, type ViewKey } from '@/lib/content'
import { Link, hrefDoc, hrefHome, hrefView, navigate } from '@/lib/router'
import { themedHtml, type Theme } from '@/lib/theme'

export function ViewPage({ viewKey, anchor, seq, pref }: { viewKey: ViewKey; anchor: string; seq: number; pref: Theme | null }) {
  const view = viewByKey.get(viewKey)!
  const frame = useRef<HTMLIFrameElement>(null)
  const anchorRef = useRef(anchor)
  anchorRef.current = anchor
  const html = useMemo(() => themedHtml(view.html, pref), [view, pref])

  const scrollTo = useCallback((a: string) => {
    frame.current?.contentWindow?.postMessage({ type: 'sr-scroll', anchor: a }, '*')
  }, [])

  useEffect(() => {
    const on = (ev: MessageEvent) => {
      if (!frame.current || ev.source !== frame.current.contentWindow) return
      const d = ev.data as { type?: string; view?: string; anchor?: string } | null
      if (!d || typeof d.type !== 'string') return
      if (d.type === 'sr-ready') scrollTo(anchorRef.current)
      else if (d.type === 'sr-nav' && d.view) navigate(d.view === 'doc' ? hrefHome : hrefView(d.view, d.anchor))
      else if (d.type === 'sr-anchor') {
        try {
          history.replaceState(null, '', hrefView(view.key, d.anchor))
        } catch {
          /* file:// pages can refuse history changes */
        }
      }
    }
    window.addEventListener('message', on)
    return () => window.removeEventListener('message', on)
  }, [view.key, scrollTo])

  useEffect(() => {
    scrollTo(anchor)
  }, [anchor, seq, scrollTo])

  const related = VIEW_CHAPTERS[viewKey]
  const note = VIEW_NOTE[viewKey]

  return (
    <div className="flex h-[calc(100dvh-3rem)] flex-col">
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 border-b px-4 py-2 text-sm lg:px-6">
        <span className="font-medium">
          <span className="mr-1.5 text-muted-foreground">图解</span>
          {VIEW_LABEL[viewKey]}
        </span>
        <span className="hidden min-w-0 flex-1 truncate text-muted-foreground md:block">{view.h1}</span>
        {related.length ? (
          <span className="flex flex-wrap items-baseline gap-1 text-muted-foreground">
            <span className="mr-1">对应章节</span>
            {related.map((slug) => {
              const c = chapterBySlug.get(slug)
              return c ? (
                <Link
                  key={slug}
                  href={hrefDoc(slug)}
                  title={c.title}
                  className="rounded-sm border px-1.5 font-mono text-xs leading-5 text-foreground hover:border-brand hover:text-brand"
                >
                  {c.num}
                </Link>
              ) : null
            })}
          </span>
        ) : null}
      </div>
      {note ? (
        <p className="border-b border-l-4 border-l-brand bg-accent/60 px-4 py-2 text-sm lg:px-6">{note}</p>
      ) : null}
      <iframe
        key={`${view.key}:${pref ?? 'system'}`}
        ref={frame}
        srcDoc={html}
        title={view.title}
        allow="clipboard-write"
        className="block w-full flex-1 border-0 bg-background"
      />
    </div>
  )
}
