#!/usr/bin/env node
// Renders the diagram sources in content/*.src.html to rendered/*.html with the visual-explainer kit renderer.
// A page is rendered again only when its source changed since its last render that passed, so a page whose
// check found problems is rendered (and reported) again on the next build.
//
//   VE_RENDER=<path to kit/render.mjs>   the renderer to use (default: the newest one in the Claude plugin cache)
//   VE_NO_CHECK=1                        skip the renderer's look at the page in a browser
//   FORCE_RENDER=1                       render every page again
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

const site = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const srcDir = path.join(site, 'content')
const outDir = path.join(site, 'rendered')

function newestFirst(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (b[i] ?? 0) - (a[i] ?? 0)
    if (d) return d
  }
  return 0
}

function findRenderer() {
  if (process.env.VE_RENDER) return fs.existsSync(process.env.VE_RENDER) ? process.env.VE_RENDER : null
  const plugins = path.join(os.homedir(), '.claude/plugins')
  const cache = path.join(plugins, 'cache/visual-explainer-marketplace/visual-explainer')
  const found = []
  if (fs.existsSync(cache)) {
    for (const version of fs.readdirSync(cache)) {
      const file = path.join(cache, version, 'kit/render.mjs')
      if (fs.existsSync(file)) found.push({ parts: version.split('.').map((x) => parseInt(x, 10) || 0), file })
    }
  }
  found.sort((a, b) => newestFirst(a.parts, b.parts))
  if (found.length) return found[0].file
  const market = path.join(plugins, 'marketplaces/visual-explainer-marketplace/plugins/visual-explainer/kit/render.mjs')
  return fs.existsSync(market) ? market : null
}

const stampOf = (file) => {
  const s = fs.statSync(file)
  return `${Math.round(s.mtimeMs)}:${s.size}`
}

const sources = fs.readdirSync(srcDir).filter((f) => f.endsWith('.src.html')).sort()
if (!sources.length) {
  console.error(`render: no *.src.html in ${path.relative(process.cwd(), srcDir)}`)
  process.exit(1)
}
fs.mkdirSync(outDir, { recursive: true })

const renderer = findRenderer()
const force = process.env.FORCE_RENDER === '1'
let failed = 0
for (const name of sources) {
  const src = path.join(srcDir, name)
  const out = path.join(outDir, name.replace(/\.src\.html$/, '.html'))
  const stampFile = `${out}.stamp`
  const stamp = stampOf(src)
  const fresh = fs.existsSync(out) && fs.existsSync(stampFile) && fs.readFileSync(stampFile, 'utf8') === stamp
  if (fresh && !force) {
    console.log(`render: ${name} unchanged`)
    continue
  }
  if (!renderer) {
    if (fs.existsSync(out)) {
      console.warn(`render: no visual-explainer renderer found; using the existing ${path.basename(out)} (it may be older than ${name})`)
      continue
    }
    console.error(
      `render: no visual-explainer renderer found, and ${path.basename(out)} was never rendered here.\n` +
        '  Install the visual-explainer plugin (0.15.0 or later, it has kit/render.mjs), or set VE_RENDER to its kit/render.mjs.',
    )
    failed++
    continue
  }
  const args = [renderer, src, '-o', out]
  if (process.env.VE_NO_CHECK === '1') args.push('--no-check')
  fs.rmSync(stampFile, { force: true })
  const run = spawnSync(process.execPath, args, { stdio: 'inherit' })
  if (run.status === 0) fs.writeFileSync(stampFile, stamp)
  else {
    console.error(`render: ${name} has problems (see above); fix the source and build again`)
    failed++
  }
}
// the renderer leaves its check sheets beside the page: they are for the one who just edited a page
if (failed) process.exit(1)
