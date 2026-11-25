import { useEffect, useMemo, useState } from 'react'
import { ArrowLeft, ArrowRight } from 'lucide-react'
import { Md } from './Markdown'
import { GROUPS, RELATED, VIEW_LABEL } from '@/lib/config'
import { chapterBySlug, chapters, content, viewByKey, type Chapter } from '@/lib/content'
import { Link, hrefDoc, hrefView } from '@/lib/router'
import { cn } from '@/lib/utils'

function useActiveSection(ids: string[]) {
  const [active, setActive] = useState('')
  useEffect(() => {
    let raf = 0
    const run = () => {
      raf = 0
      let cur = ''
      for (const id of ids) {
        const el = document.getElementById(id)
        if (el && el.getBoundingClientRect().top <= 120) cur = id
      }
      setActive(cur)
    }
    const on = () => {
      if (!raf) raf = requestAnimationFrame(run)
    }
    run()
    window.addEventListener('scroll', on, { passive: true })
    window.addEventListener('resize', on)
    return () => {
      window.removeEventListener('scroll', on)
      window.removeEventListener('resize', on)
      if (raf) cancelAnimationFrame(raf)
    }
  }, [ids])
  return active
}

function Related({ chapter, compact }: { chapter: Chapter; compact?: boolean }) {
  const list = RELATED[chapter.slug]
  if (!list?.length) return null
  return (
    <div className={cn(compact ? 'rounded border bg-card px-4 py-3' : '')}>
      <p className="mb-2 text-xs font-medium tracking-wide text-muted-foreground">相关图解</p>
      <ul className="space-y-2 text-sm">
        {list.map((r) => {
          const view = viewByKey.get(r.view)
          if (!view) return null
          return (
            <li key={r.view} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <Link href={hrefView(r.view)} className="font-medium hover:text-brand">
                {VIEW_LABEL[r.view]}
              </Link>
              {r.anchors.map((a) => {
                const s = view.sections.find((x) => x.id === a)
                return (
                  <Link
                    key={a}
                    href={hrefView(r.view, a)}
                    title={s?.title}
                    className="rounded-sm border px-1.5 text-xs leading-5 text-muted-foreground hover:border-brand hover:text-brand"
                  >
                    {s?.label ?? a}
                  </Link>
                )
              })}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function SectionList({ chapter, active }: { chapter: Chapter; active: string }) {
  return (
    <ul className="space-y-px text-sm">
      {chapter.sections.map((s) => (
        <li key={s.id}>
          <Link
            href={hrefDoc(chapter.slug, s.id)}
            className={cn(
              'flex gap-2 border-l-2 py-1 pl-3 leading-snug',
              active === s.id ? 'border-brand text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            <span className="shrink-0 font-mono text-xs leading-5">{s.num}</span>
            <span>{s.title}</span>
          </Link>
        </li>
      ))}
    </ul>
  )
}

export function DocPage({ slug, section, seq }: { slug: string; section: string; seq: number }) {
  const chapter = chapterBySlug.get(slug)!
  const idx = chapters.indexOf(chapter)
  const prev = chapters[idx - 1]
  const next = chapters[idx + 1]
  const ids = useMemo(() => chapter.sections.map((s) => s.id), [chapter])
  const active = useActiveSection(ids)
  const group = GROUPS.find((g) => g.items.some((i) => i.kind === 'chapter' && i.slug === slug))?.label

  useEffect(() => {
    const el = section ? document.getElementById(section) : null
    if (el) el.scrollIntoView({ block: 'start' })
    else window.scrollTo(0, 0)
  }, [slug, section, seq])

  return (
    <div className="flex">
      <article className="min-w-0 max-w-[48rem] flex-1 px-5 pb-16 pt-8 sm:px-8 lg:px-12">
        <p className="mb-2 text-sm text-muted-foreground">
          {group ? `${group} · ` : ''}
          {chapter.num ? `第 ${chapter.num} 章` : '设计文档'}
        </p>
        <h1 className="mb-6 flex items-baseline gap-3 font-serif text-[1.85rem] font-semibold leading-tight tracking-tight">
          {chapter.num ? <span className="font-mono text-[0.8em] font-medium text-brand">{chapter.num}</span> : null}
          <span>{chapter.title}</span>
        </h1>

        <div className="mb-8 space-y-3 xl:hidden">
          <Related chapter={chapter} compact />
          {chapter.sections.length ? (
            <details className="rounded border bg-card px-4 py-3">
              <summary className="cursor-pointer text-sm font-medium">本章 {chapter.sections.length} 节</summary>
              <div className="mt-2">
                <SectionList chapter={chapter} active={active} />
              </div>
            </details>
          ) : null}
        </div>

        <Md markdown={chapter.markdown} chapter={chapter.slug} sectionIds={ids} xref={chapter.slug !== '0'} />

        <nav aria-label="翻页" className="mt-14 grid gap-3 border-t pt-6 sm:grid-cols-2">
          {prev ? (
            <Link href={hrefDoc(prev.slug)} className="group rounded border px-4 py-3 hover:border-brand">
              <span className="flex items-center gap-1 text-xs text-muted-foreground">
                <ArrowLeft className="h-3.5 w-3.5" />
                上一章
              </span>
              <span className="mt-1 block font-medium group-hover:text-brand">
                {prev.num ? `${prev.num} ` : ''}
                {prev.title}
              </span>
            </Link>
          ) : (
            <span />
          )}
          {next ? (
            <Link href={hrefDoc(next.slug)} className="group rounded border px-4 py-3 text-right hover:border-brand">
              <span className="flex items-center justify-end gap-1 text-xs text-muted-foreground">
                下一章
                <ArrowRight className="h-3.5 w-3.5" />
              </span>
              <span className="mt-1 block font-medium group-hover:text-brand">
                {next.num ? `${next.num} ` : ''}
                {next.title}
              </span>
            </Link>
          ) : null}
        </nav>
        <p className="mt-8 text-xs text-muted-foreground">
          来源 <code className="font-mono">{content.docFile}</code> · 站点生成于 {content.generatedAt}
        </p>
      </article>

      <aside className="sticky top-12 hidden max-h-[calc(100vh-3rem)] w-64 shrink-0 space-y-8 self-start overflow-y-auto py-8 pr-6 xl:block">
        {chapter.sections.length ? (
          <div>
            <p className="mb-2 text-xs font-medium tracking-wide text-muted-foreground">本章</p>
            <SectionList chapter={chapter} active={active} />
          </div>
        ) : null}
        <Related chapter={chapter} />
      </aside>
    </div>
  )
}
