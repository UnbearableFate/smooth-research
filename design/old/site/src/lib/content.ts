import { CONTENT_B64 } from '@/generated/content'

export type Section = { id: string; num: string; title: string }
export type Chapter = { slug: string; num: string; title: string; sections: Section[]; markdown: string }
export type Block = { chapter: string; section: string; text: string }
export type ViewKey = 'workflow' | 'messages' | 'packs' | 'relations'
export type ViewSection = { id: string; label: string; title: string }
export type View = {
  key: ViewKey
  file: string
  title: string
  eyebrow: string
  h1: string
  lead: string
  meta: string
  sections: ViewSection[]
  html: string
}
export type Content = {
  generatedAt: string
  docFile: string
  doc: { title: string; intro: string; chapters: Chapter[]; blocks: Block[] }
  views: View[]
}

function decodeBase64(b64: string): string {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return new TextDecoder().decode(bytes)
}

export const content: Content = JSON.parse(decodeBase64(CONTENT_B64))
export const doc = content.doc
export const chapters = doc.chapters
export const chapterBySlug = new Map(chapters.map((c) => [c.slug, c]))
export const views = content.views
export const viewByKey = new Map(views.map((v) => [v.key, v]))
export const chapterNums = new Set(chapters.map((c) => c.num).filter(Boolean))
export const sectionNums = new Set(chapters.flatMap((c) => c.sections.map((s) => s.num)).filter(Boolean))
export const docFileName = content.docFile.split('/').pop() ?? ''

export function isViewKey(v: string): v is ViewKey {
  return viewByKey.has(v as ViewKey)
}

export function chapterName(c: Chapter): string {
  return c.num ? `${c.num} ${c.title}` : c.title
}

export function firstBlock(slug: string): string {
  return doc.blocks.find((b) => b.chapter === slug)?.text ?? ''
}
