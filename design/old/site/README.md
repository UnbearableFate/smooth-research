# Smooth Research 新设计

新设计的源文件和站点都在这个目录里。改设计就改 `content/`，然后构建一次。

```
content/                      设计的源文件，改这里
  smooth-research-redesign-2026-11-10.md       整合文档
  smooth-research-minimal-workflow.src.html    工作流页
  smooth-research-message-design.src.html      消息设计页
  smooth-research-packs.src.html               启动包和待办页
  smooth-research-data-relations.src.html      关系页
smooth-research-site.html     构建出的单文件站点，入库
rendered/                     渲染出的四张图解页，构建的中间产物，不入库
src/、scripts/                站点的代码和构建脚本
```

构建：`bash design/site/build.sh`。它做三步：

1. `scripts/render-pages.mjs` 用 visual-explainer 的渲染器把 `content/*.src.html` 渲染成 `rendered/*.html`。源文件没变的页不重新渲染。
2. `scripts/gen-content.mjs` 读整合文档和渲染好的四张页，写出 `src/generated/content.ts`。章节按 `## ` 切分，小节按 `### ` 切分，不需要手工维护目录。
3. `scripts/bundle-artifact.sh`（来自 web-artifacts-builder 技能）用 Parcel 打包，再把 JS 和 CSS 内联成一个文件，就是 `smooth-research-site.html`。

关于渲染：

- 渲染器不在仓库里。脚本自动找 Claude 插件缓存里最新的 visual-explainer（0.15.0 起有 `kit/render.mjs`），也可以用 `VE_RENDER=<kit/render.mjs 的路径>` 指定。
- 渲染器会在浏览器里看一遍页面。标注压线之类的问题会列出来，构建随之失败；改好源文件再构建。它留在 `rendered/` 里的 `.sheet*.png` 是检查图。`VE_NO_CHECK=1` 跳过这一步。
- 没有渲染器的机器上，已有的 `rendered/*.html` 会被沿用；一张都没有时构建失败。入库的 `smooth-research-site.html` 不受影响，照常能看。
- `FORCE_RENDER=1` 强制全部重新渲染。

章节和图解的对应关系、侧栏分组在 `src/lib/config.ts`。整合文档加了新的小节、想让它链接到图解时改这里。再加一张图解页，要改三处：`scripts/gen-content.mjs` 的 `VIEWS`、`src/lib/content.ts` 的 `ViewKey`、`src/lib/config.ts`。

开发：先运行 `node scripts/render-pages.mjs && node scripts/gen-content.mjs`，再运行 `pnpm dev`（Vite）。没有全局 pnpm 时用 `bin/pnpm`。
