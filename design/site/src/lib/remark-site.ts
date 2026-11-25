import { chapterNums, docFileName, sectionNums, views } from './content'

type MdNode = {
  type: string
  value?: string
  url?: string
  depth?: number
  children?: MdNode[]
  data?: { hProperties?: Record<string, unknown> }
}

export type SiteMdOptions = { sectionIds: string[]; xref: boolean }

const FILE_ROUTE: Record<string, string> = Object.fromEntries(views.map((v) => [v.file, `#/view/${v.key}`]))
FILE_ROUTE[docFileName] = '#/'

/** Inline code that names a design page becomes a link to that page in the site. */
function fileUrl(value: string): string | null {
  const m = value.match(/^(?:design\/new\/)?([\w.-]+\.(?:html|md))(?:#([\w-]+))?$/)
  if (!m || !FILE_ROUTE[m[1]]) return null
  const base = FILE_ROUTE[m[1]]
  return m[2] && base !== '#/' ? `${base}/${m[2]}` : base
}

const XREF = /第\s?(\d{1,2})\s?章|(\d{1,2})\.(\d{1,2})/g
const BEFORE = new Set('见和与及至或到、（(，,；;：:“"按入。'.split(''))
const AFTER_NUMBER = /^[\d.]|^\s?[A-Za-z%点倍个次秒分万千亿小天周年月元条字句]/

function xrefUrl(m: RegExpExecArray, s: string): string | null {
  const a = m.index
  const b = a + m[0].length
  if (m[1]) return chapterNums.has(m[1]) ? `#/doc/${m[1]}` : null
  const prev = a > 0 ? s[a - 1] : ''
  if (prev && /[\d.+\-−~A-Za-z]/.test(prev)) return null
  if (AFTER_NUMBER.test(s.slice(b, b + 2))) return null
  let j = a - 1
  if (j >= 0 && s[j] === ' ') j--
  if (j >= 0 && !BEFORE.has(s[j])) return null
  const num = `${m[2]}.${m[3]}`
  return sectionNums.has(num) ? `#/doc/${m[2]}/s-${m[2]}-${m[3]}` : null
}

function splitText(value: string): MdNode[] | null {
  const out: MdNode[] = []
  let last = 0
  XREF.lastIndex = 0
  for (let m = XREF.exec(value); m; m = XREF.exec(value)) {
    const url = xrefUrl(m, value)
    if (!url) continue
    if (m.index > last) out.push({ type: 'text', value: value.slice(last, m.index) })
    out.push({ type: 'link', url, children: [{ type: 'text', value: m[0] }], data: { hProperties: { className: ['xref'] } } })
    last = m.index + m[0].length
  }
  if (!out.length) return null
  if (last < value.length) out.push({ type: 'text', value: value.slice(last) })
  return out
}

const toText = (n: MdNode): string => (typeof n.value === 'string' ? n.value : (n.children ?? []).map(toText).join(''))

function linkify(node: MdNode) {
  if (!node.children) return
  const out: MdNode[] = []
  for (const k of node.children) {
    if (k.type === 'text' && k.value) {
      const parts = splitText(k.value)
      if (parts) {
        out.push(...parts)
        continue
      }
    } else if (k.type !== 'link' && k.type !== 'linkReference') {
      linkify(k)
    }
    out.push(k)
  }
  node.children = out
}

/**
 * Section references such as "（8.5）" and "第 9 章" become links. A block that
 * mentions the 11-09 document is left alone, because its numbers may point there.
 */
function xrefBlocks(node: MdNode) {
  if (node.type === 'paragraph' || node.type === 'tableCell') {
    if (!toText(node).includes('11-09')) linkify(node)
    return
  }
  if (node.type === 'heading') return
  node.children?.forEach(xrefBlocks)
}

function linkCode(node: MdNode) {
  if (!node.children || node.type === 'link') return
  node.children = node.children.map((k) => {
    if (k.type === 'inlineCode' && k.value) {
      const url = fileUrl(k.value)
      if (url) return { type: 'link', url, children: [k] }
    }
    linkCode(k)
    return k
  })
}

export function remarkSite(options: SiteMdOptions) {
  return (tree: MdNode) => {
    let k = 0
    const visit = (n: MdNode) => {
      if (n.type === 'heading' && n.depth === 3) {
        const id = options.sectionIds[k++]
        if (id) n.data = { ...n.data, hProperties: { ...(n.data?.hProperties ?? {}), id } }
      }
      n.children?.forEach(visit)
    }
    visit(tree)
    linkCode(tree)
    if (options.xref) xrefBlocks(tree)
  }
}
