import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'

const FONTS =
  'https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@600;700&display=swap'
const ICON =
  "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='4' fill='#b4531f'/><path d='M16 11v5M16 16l-6 6M16 16l6 6' stroke='#fff' stroke-width='2.2' fill='none'/><circle cx='16' cy='9.5' r='3' fill='#fff'/><circle cx='9.5' cy='22.5' r='3' fill='#fff'/><circle cx='22.5' cy='22.5' r='3' fill='#fff'/></svg>"

function addLink(rel: string, href: string) {
  const l = document.createElement('link')
  l.rel = rel
  l.href = href
  document.head.appendChild(l)
}
addLink('stylesheet', FONTS)
addLink('icon', `data:image/svg+xml,${encodeURIComponent(ICON)}`)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
