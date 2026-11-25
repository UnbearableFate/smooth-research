import { Children, isValidElement, useMemo, type ReactNode } from 'react'
import Markdown, { type Components, type Options } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { remarkSite } from '@/lib/remark-site'
import { Link, hrefDoc, hrefView } from '@/lib/router'
import { SECTION_VIEW, VIEW_LABEL } from '@/lib/config'
import { viewByKey } from '@/lib/content'
import { cn } from '@/lib/utils'

const EMPTY: string[] = []

function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) return textOf(node.props.children)
  return ''
}

function makeComponents(chapter: string): Components {
  return {
    a({ href = '', children, className }) {
      if (href.startsWith('#/')) return <Link href={href} className={className}>{children}</Link>
      const external = /^https?:/i.test(href)
      return (
        <a href={href} className={className} {...(external ? { target: '_blank', rel: 'noopener noreferrer' } : {})}>
          {children}
        </a>
      )
    },
    table({ children }) {
      return (
        <div className="table-wrap">
          <table>{children}</table>
        </div>
      )
    },
    h3({ id, children }) {
      const first = Children.toArray(children)[0]
      const m = typeof first === 'string' ? first.match(/^(\d+\.\d+)\s+/) : null
      const rest = m ? [first.toString().slice(m[0].length), ...Children.toArray(children).slice(1)] : children
      const drawn = id ? SECTION_VIEW[id] : undefined
      const sec = drawn ? viewByKey.get(drawn[0])?.sections.find((s) => s.id === drawn[1]) : undefined
      return (
        <h3 id={id}>
          {id ? (
            <Link href={hrefDoc(chapter, id)} className="h-link" aria-label={`链接到 ${textOf(children)}`}>
              {m ? <span className="sec-num">{m[1]}</span> : null}
              {rest}
            </Link>
          ) : (
            children
          )}
          {drawn ? (
            <Link href={hrefView(drawn[0], drawn[1])} className="h-view" title={sec?.title}>
              图解：{VIEW_LABEL[drawn[0]]} · {sec?.label ?? drawn[1]}
            </Link>
          ) : null}
        </h3>
      )
    },
  }
}

export function Md({
  markdown,
  chapter = '',
  sectionIds = EMPTY,
  xref = true,
  className,
}: {
  markdown: string
  chapter?: string
  sectionIds?: string[]
  xref?: boolean
  className?: string
}) {
  const plugins = useMemo(
    () => [remarkGfm, [remarkSite, { sectionIds, xref }]] as NonNullable<Options['remarkPlugins']>,
    [sectionIds, xref],
  )
  const components = useMemo(() => makeComponents(chapter), [chapter])
  return (
    <div className={cn('prose-sr', className)}>
      <Markdown remarkPlugins={plugins} components={components}>
        {markdown}
      </Markdown>
    </div>
  )
}
