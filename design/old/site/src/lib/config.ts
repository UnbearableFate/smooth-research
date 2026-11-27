import type { ViewKey } from './content'

export type NavItem =
  | { kind: 'home' }
  | { kind: 'chapter'; slug: string }
  | { kind: 'view'; view: ViewKey; anchor?: string; label: string; note?: string }

const ch = (...slugs: string[]): NavItem[] => slugs.map((slug) => ({ kind: 'chapter', slug }))

export const GROUPS: { label: string; items: NavItem[] }[] = [
  { label: '总览', items: [{ kind: 'home' }, ...ch('summary', '0', '1', '2')] },
  { label: '角色和流程', items: ch('3', '4', '5', '6', '7') },
  {
    label: '消息和检查',
    items: [...ch('8'), { kind: 'view', view: 'messages', anchor: 'examples', label: '消息范例' }, ...ch('9')],
  },
  { label: '数据和恢复', items: ch('10', '11', '12') },
  { label: '决定和计划', items: ch('13', '14', '15', '16', '17') },
  {
    label: '图解',
    items: [
      { kind: 'view', view: 'workflow', label: '工作流' },
      { kind: 'view', view: 'messages', label: '消息设计' },
      { kind: 'view', view: 'packs', label: '启动包和待办' },
      { kind: 'view', view: 'relations', label: '研究记录关系' },
    ],
  },
]

export const VIEW_LABEL: Record<ViewKey, string> = {
  workflow: '工作流',
  messages: '消息设计',
  packs: '启动包和待办',
  relations: '研究记录关系',
}

export const VIEW_NOTE: Partial<Record<ViewKey, string>> = {}

/** Diagram sections that draw each chapter. */
export const RELATED: Record<string, { view: ViewKey; anchors: string[] }[]> = {
  '2': [{ view: 'workflow', anchors: ['roles'] }],
  '3': [{ view: 'workflow', anchors: ['roles'] }],
  '4': [{ view: 'workflow', anchors: ['scientist'] }],
  '5': [{ view: 'workflow', anchors: ['code'] }],
  '6': [{ view: 'workflow', anchors: ['handover'] }],
  '7': [{ view: 'workflow', anchors: ['roles', 'removed'] }],
  '8': [
    { view: 'messages', anchors: ['format', 'locators', 'notices', 'pending', 'review', 'examples'] },
    { view: 'packs', anchors: ['stores', 'inbox'] },
    { view: 'workflow', anchors: ['messages'] },
  ],
  '9': [{ view: 'workflow', anchors: ['checks'] }],
  '10': [{ view: 'relations', anchors: [] }],
  '11': [
    { view: 'packs', anchors: ['anatomy', 'coordinator', 'scientist', 'researcher', 'others', 'wake'] },
    { view: 'workflow', anchors: ['startup'] },
  ],
  '13': [
    { view: 'workflow', anchors: ['open'] },
    { view: 'messages', anchors: ['decide'] },
    { view: 'packs', anchors: ['decide'] },
  ],
  '14': [
    { view: 'workflow', anchors: ['open'] },
    { view: 'messages', anchors: ['decide'] },
  ],
}

/** Diagram section that draws one doc section. */
export const SECTION_VIEW: Record<string, [ViewKey, string]> = {
  's-4-8': ['workflow', 'scientist'],
  's-6-4': ['workflow', 'handover'],
  's-6-5': ['workflow', 'handover'],
  's-6-6': ['workflow', 'handover'],
  's-8-3': ['workflow', 'messages'],
  's-8-5': ['messages', 'format'],
  's-8-6': ['messages', 'locators'],
  's-8-7': ['messages', 'notices'],
  's-8-8': ['messages', 'pending'],
  's-8-9': ['messages', 'review'],
  's-8-10': ['messages', 'examples'],
  's-8-11': ['packs', 'stores'],
  's-9-1': ['workflow', 'checks'],
  's-9-2': ['workflow', 'checks'],
  's-9-3': ['workflow', 'checks'],
  's-11-1': ['workflow', 'startup'],
  's-11-2': ['workflow', 'startup'],
  's-11-3': ['workflow', 'startup'],
  's-11-5': ['packs', 'anatomy'],
}

export const VIEW_CHAPTERS: Record<ViewKey, string[]> = (() => {
  const out: Record<ViewKey, string[]> = { workflow: [], messages: [], packs: [], relations: [] }
  for (const [slug, list] of Object.entries(RELATED)) for (const r of list) if (!out[r.view].includes(slug)) out[r.view].push(slug)
  return out
})()
