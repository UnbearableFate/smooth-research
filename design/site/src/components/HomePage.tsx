import { useEffect } from 'react'
import { Md } from './Markdown'
import { GROUPS, VIEW_LABEL, VIEW_NOTE } from '@/lib/config'
import { chapterBySlug, chapters, content, doc, firstBlock, views } from '@/lib/content'
import { Link, hrefDoc, hrefView } from '@/lib/router'

const START = ['2', '8', '13', '14']

export function HomePage({ seq }: { seq: number }) {
  useEffect(() => {
    window.scrollTo(0, 0)
  }, [seq])

  const [lead, rest] = doc.title.includes('：') ? doc.title.split(/：(.*)/s) : [doc.title, '']
  const summary = chapterBySlug.get('summary')

  return (
    <div className="max-w-[48rem] px-5 pb-16 pt-10 sm:px-8 lg:px-12">
      <p className="mb-3 font-mono text-xs text-muted-foreground">design/site/content · 整合文档和图解</p>
      <h1 className="font-serif text-[2.1rem] font-bold leading-[1.25] tracking-tight">
        {lead}
        {rest ? <span className="mt-1 block text-[0.72em] font-semibold text-muted-foreground">{rest}</span> : null}
      </h1>
      <Md markdown={doc.intro} xref={false} className="mt-5 text-sm text-muted-foreground [&_p]:leading-relaxed" />

      {summary ? (
        <section className="mt-10">
          <h2 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground">摘要</h2>
          <Md markdown={summary.markdown} chapter="summary" className="text-[1.05rem]" />
        </section>
      ) : null}

      <section className="mt-12">
        <h2 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground">四张图解</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          {views.map((v) => (
            <Link key={v.key} href={hrefView(v.key)} className="group flex flex-col rounded border bg-card p-4 hover:border-brand">
              <span className="flex items-baseline justify-between gap-2 text-sm font-medium text-brand">
                {VIEW_LABEL[v.key]}
                {VIEW_NOTE[v.key] ? <span className="text-xs font-normal text-muted-foreground">定稿之前</span> : null}
              </span>
              <span className="mt-2 text-[0.95rem] font-medium leading-snug group-hover:text-brand">{v.h1}</span>
              <span className="mt-3 flex flex-wrap gap-1">
                {v.sections.map((s) => (
                  <span key={s.id} className="rounded-sm border px-1.5 text-xs leading-5 text-muted-foreground">
                    {s.label}
                  </span>
                ))}
              </span>
            </Link>
          ))}
        </div>
      </section>

      <section className="mt-12">
        <h2 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground">从这几章读起</h2>
        <ol className="divide-y rounded border bg-card">
          {START.map((slug) => {
            const c = chapterBySlug.get(slug)
            if (!c) return null
            return (
              <li key={slug}>
                <Link href={hrefDoc(slug)} className="group flex gap-4 px-4 py-3 hover:bg-accent/60">
                  <span className="w-6 shrink-0 pt-0.5 text-right font-mono text-sm text-brand">{c.num}</span>
                  <span className="min-w-0">
                    <span className="block font-medium group-hover:text-brand">{c.title}</span>
                    <span className="mt-0.5 line-clamp-2 block text-sm text-muted-foreground">{firstBlock(slug)}</span>
                  </span>
                </Link>
              </li>
            )
          })}
        </ol>
      </section>

      <section className="mt-12">
        <h2 className="mb-3 text-xs font-medium tracking-wide text-muted-foreground">全部章节</h2>
        <div className="grid gap-x-8 gap-y-6 sm:grid-cols-2">
          {GROUPS.filter((g) => g.items.some((i) => i.kind === 'chapter')).map((g) => (
            <div key={g.label}>
              <p className="mb-1.5 text-sm font-medium">{g.label}</p>
              <ul className="space-y-1 text-sm">
                {g.items.map((i) => {
                  if (i.kind !== 'chapter') return null
                  const c = chapterBySlug.get(i.slug)
                  if (!c) return null
                  return (
                    <li key={c.slug}>
                      <Link href={hrefDoc(c.slug)} className="flex gap-2 text-muted-foreground hover:text-brand">
                        <span className="w-5 shrink-0 text-right font-mono text-xs leading-6">{c.num}</span>
                        <span className="leading-6">
                          {c.title}
                          {c.sections.length ? <span className="ml-1.5 text-xs opacity-70">{c.sections.length} 节</span> : null}
                        </span>
                      </Link>
                    </li>
                  )
                })}
              </ul>
            </div>
          ))}
        </div>
      </section>

      <p className="mt-12 border-t pt-4 text-xs text-muted-foreground">
        {chapters.length} 章 · 来源 <code className="font-mono">{content.docFile}</code> 和同一目录下的四张图解 · 站点生成于{' '}
        {content.generatedAt}
      </p>
    </div>
  )
}
