#!/usr/bin/env node
// Reads the design document in content/ and the diagram pages rendered from content/*.src.html (rendered/,
// written by scripts/render-pages.mjs), and writes src/generated/content.ts.
// The content is stored as base64 so that no "</script>" in a page can end the
// inline script of the bundled site.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const site = path.resolve(here, '..')
const srcDir = path.join(site, 'content')
const pageDir = path.join(site, 'rendered')
const outDir = path.join(site, 'src/generated')

const DOC_FILE = 'smooth-research-redesign-2026-11-10.md'
const VIEWS = [
  { key: 'workflow', file: 'smooth-research-minimal-workflow.html' },
  { key: 'messages', file: 'smooth-research-message-design.html' },
  { key: 'packs', file: 'smooth-research-packs.html' },
  { key: 'relations', file: 'smooth-research-data-relations.html' },
]
const FILES = Object.fromEntries(VIEWS.map((v) => [v.file, v.key]))
FILES[DOC_FILE] = 'doc'

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' }
function decode(s) {
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e) => {
    if (e[0] === '#') return String.fromCodePoint(e[1] === 'x' || e[1] === 'X' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10))
    return ENTITIES[e.toLowerCase()] ?? m
  })
}
const text = (html) => decode(html.replace(/<[^>]+>/g, '')).replace(/\s+/g, ' ').trim()

function plain(md) {
  return md
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/, '')
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\*\*|__|`/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function parseDoc(md) {
  const lines = md.replace(/\r\n/g, '\n').split('\n')
  let title = ''
  let i = 0
  for (; i < lines.length; i++) {
    if (lines[i].startsWith('# ')) {
      title = lines[i].slice(2).trim()
      i++
      break
    }
  }
  const intro = []
  const chapters = []
  let cur = null
  let fence = false
  for (; i < lines.length; i++) {
    const l = lines[i]
    const isFence = /^\s*(```|~~~)/.test(l)
    if (!fence && l.startsWith('## ')) {
      const head = l.slice(3).trim()
      const m = head.match(/^(\d+)\.\s+(.*)$/)
      const slug = m ? m[1] : head === '摘要' ? 'summary' : `part-${chapters.length + 1}`
      cur = { slug, num: m ? m[1] : '', title: m ? m[2] : head, lines: [], sections: [] }
      chapters.push(cur)
      continue
    }
    if (isFence) fence = !fence
    if (!cur) {
      intro.push(l)
      continue
    }
    if (!fence && l.startsWith('### ')) {
      const head = l.slice(4).trim()
      const m = head.match(/^(\d+)\.(\d+)\s+(.*)$/)
      const id = m ? `s-${m[1]}-${m[2]}` : `s-${cur.slug}-${cur.sections.length + 1}`
      cur.sections.push({ id, num: m ? `${m[1]}.${m[2]}` : '', title: m ? m[3] : head })
    }
    cur.lines.push(l)
  }

  const blocks = []
  for (const c of chapters) {
    let section = ''
    let buf = []
    let inFence = false
    const flush = () => {
      const t = plain(buf.join(' '))
      if (t) blocks.push({ chapter: c.slug, section, text: t })
      buf = []
    }
    let k = 0
    for (const l of c.lines) {
      if (/^\s*(```|~~~)/.test(l)) {
        if (inFence) flush()
        else flush()
        inFence = !inFence
        continue
      }
      if (inFence) {
        buf.push(l)
        continue
      }
      if (l.startsWith('### ')) {
        flush()
        section = c.sections[k++]?.id ?? ''
        continue
      }
      if (!l.trim()) {
        flush()
        continue
      }
      if (/^\s*\|/.test(l)) {
        flush()
        if (/^\s*\|?\s*:?-{2,}/.test(l)) continue
        const cells = l.trim().replace(/^\||\|$/g, '').split('|').map((x) => plain(x)).filter(Boolean)
        if (cells.length) blocks.push({ chapter: c.slug, section, text: cells.join(' · ') })
        continue
      }
      if (/^\s*(?:[-*+]|\d+\.)\s+/.test(l)) flush()
      buf.push(l)
    }
    flush()
  }

  return {
    title,
    intro: intro.join('\n').trim(),
    chapters: chapters.map((c) => ({
      slug: c.slug,
      num: c.num,
      title: c.title,
      sections: c.sections,
      markdown: c.lines.join('\n').trim(),
    })),
    blocks,
  }
}

const storagePatch = (key) =>
  `(function(){try{var P=Storage.prototype,g=P.getItem,s=P.setItem,r=P.removeItem;` +
  `function k(x){return typeof x==="string"&&x.indexOf("ve-page:")===0?"ve-page:sr-site/${key}":x}` +
  `P.getItem=function(x){return g.call(this,k(x))};P.setItem=function(x,y){return s.call(this,k(x),y)};` +
  `P.removeItem=function(x){return r.call(this,k(x))}}catch(e){}})();`

const bridge = (key) => `(function(){
if (window.parent === window) return;
var VIEW = ${JSON.stringify(key)};
var FILES = ${JSON.stringify(FILES)};
function post(m){ try { window.parent.postMessage(m, "*"); } catch (e) {} }
function go(id){
  if (!id) { window.scrollTo(0, 0); return; }
  var el = document.getElementById(id);
  if (el) el.scrollIntoView({ block: "start" });
}
var want = null;
["wheel", "touchstart", "keydown", "mousedown"].forEach(function(t){
  window.addEventListener(t, function(){ want = null; }, { capture: true, passive: true });
});
function settle(id){
  want = id;
  go(id);
  var again = function(){ if (want === id) go(id); };
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(again);
  if (document.readyState !== "complete") window.addEventListener("load", again, { once: true });
  setTimeout(again, 400);
  setTimeout(function(){ again(); if (want === id) want = null; }, 1500);
}
document.addEventListener("click", function(ev){
  if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
  var t = ev.target;
  var a = t && t.closest ? t.closest("a[href]") : null;
  if (!a) return;
  var href = a.getAttribute("href") || "";
  if (href.charAt(0) === "#") {
    ev.preventDefault();
    var id = decodeURIComponent(href.slice(1));
    go(id);
    post({ type: "sr-anchor", view: VIEW, anchor: id });
    return;
  }
  var m = href.match(/^(?:\\.\\/)?(?:design\\/(?:new|site\\/content|site\\/rendered)\\/)?([\\w.-]+\\.(?:html|md))(?:#(.*))?$/);
  if (m && FILES[m[1]]) {
    ev.preventDefault();
    post({ type: "sr-nav", view: FILES[m[1]], anchor: m[2] ? decodeURIComponent(m[2]) : "" });
    return;
  }
  if (/^(https?:|mailto:)/i.test(href)) {
    ev.preventDefault();
    window.open(href, "_blank", "noopener");
  }
}, true);
document.addEventListener("keydown", function(ev){
  if ((ev.key === "k" || ev.key === "K") && (ev.metaKey || ev.ctrlKey)) {
    ev.preventDefault();
    post({ type: "sr-search" });
  }
});
window.addEventListener("message", function(ev){
  var d = ev.data;
  if (d && d.type === "sr-scroll") settle(d.anchor || "");
});
post({ type: "sr-ready", view: VIEW });
})();`

function inject(html, key) {
  let out = html.replace(/<head(\s[^>]*)?>/i, (m) => `${m}<script>${storagePatch(key)}</script>`)
  const at = out.toLowerCase().lastIndexOf('</body>')
  const tag = `<script>${bridge(key)}</script>`
  out = at >= 0 ? out.slice(0, at) + tag + out.slice(at) : out + tag
  return out
}

function parseView(v) {
  const html = fs.readFileSync(path.join(pageDir, v.file), 'utf8')
  const pick = (re) => {
    const m = html.match(re)
    return m ? text(m[1]) : ''
  }
  const nav = {}
  const navHtml = html.match(/<nav class="ve-toc"[^>]*>([\s\S]*?)<\/nav>/)
  if (navHtml) for (const m of navHtml[1].matchAll(/<a href="#([^"]+)"[^>]*>([\s\S]*?)<\/a>/g)) nav[m[1]] = text(m[2])
  const sections = []
  for (const m of html.matchAll(/<section\b[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const rest = html.slice(m.index)
    const h2 = rest.match(/<h2[^>]*>([\s\S]*?)<\/h2>/)
    sections.push({ id: m[1], label: nav[m[1]] ?? m[1], title: h2 ? text(h2[1]) : '' })
  }
  return {
    key: v.key,
    file: v.file,
    title: pick(/<title>([\s\S]*?)<\/title>/),
    eyebrow: pick(/<p class="eyebrow">([\s\S]*?)<\/p>/),
    h1: pick(/<h1[^>]*>([\s\S]*?)<\/h1>/),
    lead: pick(/<p class="lead">([\s\S]*?)<\/p>/),
    meta: pick(/<p class="meta">([\s\S]*?)<\/p>/),
    sections,
    html: inject(html, v.key),
  }
}

const doc = parseDoc(fs.readFileSync(path.join(srcDir, DOC_FILE), 'utf8'))
const views = VIEWS.map(parseView)
const content = {
  generatedAt: new Date().toLocaleString('sv-SE').slice(0, 16),
  docFile: `design/site/content/${DOC_FILE}`,
  doc,
  views,
}

fs.mkdirSync(outDir, { recursive: true })
const b64 = Buffer.from(JSON.stringify(content), 'utf8').toString('base64')
fs.writeFileSync(
  path.join(outDir, 'content.ts'),
  `// Generated by scripts/gen-content.mjs from design/site/content. Do not edit.\nexport const CONTENT_B64 = ${JSON.stringify(b64)}\n`,
)
const sec = doc.chapters.reduce((n, c) => n + c.sections.length, 0)
console.log(
  `content: ${doc.chapters.length} chapters, ${sec} sections, ${doc.blocks.length} search blocks; ` +
    views.map((v) => `${v.key} ${v.sections.length} sections ${Math.round(v.html.length / 1024)}k`).join(', '),
)
