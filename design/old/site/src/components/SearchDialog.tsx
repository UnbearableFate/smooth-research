import { useMemo, useState, type ReactNode } from 'react'
import { FileText, Hash, Workflow } from 'lucide-react'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { VIEW_LABEL } from '@/lib/config'
import { chapterBySlug, chapterName, chapters, doc, views, type ViewKey } from '@/lib/content'
import { hrefDoc, hrefView, navigate } from '@/lib/router'

type Kind = 'chapter' | 'section' | 'text' | 'view'
type Hit = { id: string; kind: Kind; href: string; title: ReactNode; meta?: string; snippet?: ReactNode }
type ViewEntry = { view: ViewKey; anchor: string; title: string; text: string }

const MAX_TEXT = 40
const MAX_VIEW = 24
const clean = (s: string | null | undefined) => (s ?? '').replace(/\s+/g, ' ').trim()
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

let viewCache: ViewEntry[] | null = null

/** Text of the diagram pages, read once from their HTML without running it. */
function viewIndex(): ViewEntry[] {
  if (viewCache) return viewCache
  const out: ViewEntry[] = []
  const parser = new DOMParser()
  for (const v of views) {
    const d = parser.parseFromString(v.html, 'text/html')
    d.querySelectorAll('script, style, template, nav').forEach((n) => n.remove())
    const header = d.querySelector('header')
    if (header) out.push({ view: v.key, anchor: '', title: v.h1, text: clean(header.textContent) })
    d.querySelectorAll('section[id]').forEach((sec) => {
      const label = v.sections.find((s) => s.id === sec.id)?.label ?? sec.id
      const h2 = clean(sec.querySelector('h2')?.textContent)
      const figs = Array.from(sec.querySelectorAll('figure'))
      for (const f of figs) {
        const cap = clean(f.querySelector('figcaption')?.textContent).replace(/^(图\s*\d+)(?=\S)/, '$1 ')
        out.push({ view: v.key, anchor: f.id || sec.id, title: `${label} · ${cap || h2}`, text: clean(f.textContent) })
      }
      figs.forEach((f) => f.remove())
      out.push({ view: v.key, anchor: sec.id, title: `${label} · ${h2}`, text: clean(sec.textContent) })
    })
  }
  viewCache = out
  return out
}

function has(text: string, terms: string[]) {
  const t = text.toLowerCase()
  return terms.every((x) => t.includes(x))
}

function mark(s: string, terms: string[]): ReactNode {
  const re = new RegExp(`(${terms.map(escape).join('|')})`, 'gi')
  return s.split(re).map((p, i) => (i % 2 ? <mark key={i}>{p}</mark> : p))
}

function snip(text: string, terms: string[]): ReactNode {
  const i = Math.max(0, text.toLowerCase().indexOf(terms[0]))
  const start = Math.max(0, i - 28)
  const end = Math.min(text.length, start + 120)
  return mark(`${start > 0 ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`, terms)
}

function search(terms: string[]): { label: string; hits: Hit[] }[] {
  if (!terms.length) {
    return [
      {
        label: '章节',
        hits: chapters.map((c) => ({ id: `c:${c.slug}`, kind: 'chapter', href: hrefDoc(c.slug), title: chapterName(c) })),
      },
      {
        label: '图解',
        hits: views.map((v) => ({ id: `v:${v.key}`, kind: 'view', href: hrefView(v.key), title: VIEW_LABEL[v.key], meta: v.h1 })),
      },
    ]
  }
  const titles: Hit[] = []
  for (const c of chapters) {
    const name = chapterName(c)
    if (has(name, terms)) titles.push({ id: `c:${c.slug}`, kind: 'chapter', href: hrefDoc(c.slug), title: mark(name, terms) })
    for (const s of c.sections) {
      const t = `${s.num} ${s.title}`
      if (has(t, terms)) titles.push({ id: `s:${s.id}`, kind: 'section', href: hrefDoc(c.slug, s.id), title: mark(t, terms), meta: name })
    }
  }
  const text: Hit[] = []
  doc.blocks.forEach((b, i) => {
    if (text.length >= MAX_TEXT || !has(b.text, terms)) return
    const c = chapterBySlug.get(b.chapter)
    if (!c) return
    const s = c.sections.find((x) => x.id === b.section)
    text.push({
      id: `b:${i}`,
      kind: 'text',
      href: hrefDoc(b.chapter, b.section),
      title: s ? `${s.num} ${s.title}` : chapterName(c),
      snippet: snip(b.text, terms),
    })
  })
  const pics: Hit[] = []
  viewIndex().forEach((e, i) => {
    if (pics.length >= MAX_VIEW || !has(e.text, terms)) return
    pics.push({
      id: `p:${i}`,
      kind: 'view',
      href: hrefView(e.view, e.anchor),
      title: `${VIEW_LABEL[e.view]} · ${e.title}`,
      snippet: snip(e.text, terms),
    })
  })
  return [
    { label: '章节', hits: titles },
    { label: text.length >= MAX_TEXT ? `正文（前 ${MAX_TEXT} 条）` : '正文', hits: text },
    { label: '图解', hits: pics },
  ].filter((g) => g.hits.length)
}

const ICON: Record<Kind, typeof FileText> = { chapter: FileText, section: Hash, text: FileText, view: Workflow }

export function SearchDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const [query, setQuery] = useState('')
  const terms = useMemo(() => query.toLowerCase().split(/\s+/).filter(Boolean), [query])
  const groups = useMemo(() => (open ? search(terms) : []), [open, terms])

  const change = (o: boolean) => {
    if (!o) setQuery('')
    onOpenChange(o)
  }

  return (
    <Dialog open={open} onOpenChange={change}>
      <DialogContent
        aria-describedby={undefined}
        className="top-[8vh] max-w-2xl translate-y-0 gap-0 overflow-hidden rounded border p-0 data-[state=closed]:slide-out-to-top-2 data-[state=open]:slide-in-from-top-2 sm:rounded"
      >
        <DialogTitle className="sr-only">搜索文档和图解</DialogTitle>
        <Command shouldFilter={false} className="rounded-none">
          <CommandInput value={query} onValueChange={setQuery} placeholder="搜索章节、正文和图解，空格分开多个词" className="h-12 pr-8" />
          <CommandList className="max-h-[min(65vh,34rem)]">
            <CommandEmpty>没有找到“{query}”。</CommandEmpty>
            {groups.map((g) => (
              <CommandGroup key={g.label} heading={g.label}>
                {g.hits.map((h) => {
                  const Icon = ICON[h.kind]
                  return (
                    <CommandItem
                      key={h.id}
                      value={h.id}
                      onSelect={() => {
                        change(false)
                        navigate(h.href)
                      }}
                      className="items-start gap-3 rounded-sm py-2"
                    >
                      <Icon className="mt-0.5 text-muted-foreground" />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate">{h.title}</span>
                        {h.meta ? <span className="block truncate text-xs text-muted-foreground">{h.meta}</span> : null}
                        {h.snippet ? (
                          <span className="mt-0.5 line-clamp-2 block text-xs leading-relaxed text-muted-foreground">{h.snippet}</span>
                        ) : null}
                      </span>
                    </CommandItem>
                  )
                })}
              </CommandGroup>
            ))}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  )
}
