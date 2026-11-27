"""The research page, docs/research.html, drawn from the records (design 8.12).

Write commands never touch it: the watcher redraws it when the records changed and every agents.page_minutes; `rl
view` draws it at once. The records are read in one short read transaction and the lock is released before the
page is rendered. The page is generated, ignored by git.

Four parts: Now (each hypothesis at work: its step, who it waits on and since when; the last 24 hours), Findings
(phase conclusions first, then reviewed hypothesis conclusions, then accepted task conclusions of hypotheses still
at work, marked as interim), Waiting on you (the user's inbox), and the tree with its costs, drawn by the
bypass-lora template's script from the records as JSON. While a tree draft waits for the user's approval
(research/tree-draft/tree.yaml, not a record), it comes first, in full, marked as a draft (design 2026-11-27, 7.2).
"Waited for" is computed in the page when it is opened and every minute; a page older than two redraw intervals
says when it was generated and that rl view refreshes it.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from importlib import resources

from . import inbox, tree
from .project import agents_value
from .research import children, parent_of
from .reviews import open_findings
from .schema import TASK_DONE
from .store import Store, Tx
from .usage_view import task_usage_short
from .util import atomic_write, now_utc, parse_time

PAGE = "research.html"


def _asset(name: str) -> str:
    return resources.files("smooth_research").joinpath("page_assets", name).read_text(encoding="utf-8")


def esc(text) -> str:
    return html.escape(str(text if text is not None else ""))


def _step(tx: Tx, t: dict) -> str:
    """Where a hypothesis task is: designing, design review n, runs k/n, analysing, conclusion review, merging."""
    if t["state"] == "queued":
        return "排队中"
    if t["state"] == "open":
        return "已放行，等科学家"
    if t["state"] == "delivered":
        return "结论已交付，等协调者接收"
    kids = [tx.require(e["id"]) for e in tx.inc(t["id"], "part_of")]
    for d in tx.all("Decision", task_id=t["id"]):
        if d["state"] == "draft":
            return "结论审核中"
    live = [k for k in kids if k["state"] not in TASK_DONE]
    if not live:
        return "设计中" if not kids else "分析中"
    k = live[0]
    if k["state"] == "merging":
        return "合并中"
    rounds = [r for r in tx.all("Ticket", task_id=k["id"]) if r.get("review_kind") == "design"]
    if k.get("holder", "").startswith("scientist-"):
        if rounds and rounds[-1]["status"] in ("waiting", "answered"):
            return f"设计审核第 {len(rounds[-1].get('passes') or [])} 次"
        return "设计中"
    atts = [a for r in tx.all("Run", task_id=k["id"]) for a in tx.all("Attempt", run_id=r["id"])]
    ended = [a for a in atts if a["execution_state"] == "ended"]
    running = [a for a in atts if a["execution_state"] != "ended"]
    if running:
        end = max((a.get("expected_end") or "" for a in running), default="")
        return f"运行 {len(ended)}/{len(atts)}" + (f"，预计 {end[11:16]} UTC 结束" if end else "")
    if k["state"] == "delivered":
        return "任务结论已交付，等科学家验收"
    return "运行中" if k["kind"] == "execute" else "准备中"


def collect(store: Store) -> dict:
    """Everything the page shows, read in one transaction."""
    config = store.project.config
    with store.read() as tx:
        items = inbox.all_items(tx, config)
        hyps = {h["id"]: h for h in tx.all("Hypothesis")}
        exps = {e["id"]: e for e in tx.all("Experiment")}
        tasks = tx.all("Task")
        decisions = tx.all("Decision")
        events = tx.all("Event")
        phases = tx.all("Phase")
        meta = tx.meta()
        kids = {hid: [c["id"] for c in children(tx, hid)] for hid in hyps}
        parents = {hid: parent_of(tx, hid) for hid in hyps}
        tests = {eid: [e["id"] for e in tx.out(eid, "tests")] for eid in exps}
        decided = {d["id"]: [e["id"] for e in tx.out(d["id"], "decides")] for d in decisions}
        targets = {t["id"]: [e["id"] for e in tx.out(t["id"], "targets")] for t in tasks}
        part = {t["id"]: next((e["id"] for e in tx.out(t["id"], "part_of")), None) for t in tasks}
        in_phase = {t["id"]: next((e["id"] for e in tx.out(t["id"], "in_phase")), None) for t in tasks}
        usage = {t["id"]: task_usage_short(tx, t) for t in tasks if t["kind"] == "hypothesis"}
        steps = {t["id"]: _step(tx, t) for t in tasks if t["kind"] == "hypothesis" and t["state"] not in TASK_DONE}
        exp_task = {}
        for t in tasks:
            for x in targets[t["id"]]:
                if x in exps and t["kind"] == "execute":
                    exp_task.setdefault(x, []).append(t)
        running = {eid: any(a["execution_state"] != "ended" for t in exp_task.get(eid, [])
                            for r in tx.all("Run", task_id=t["id"]) for a in tx.all("Attempt", run_id=r["id"]))
                   for eid in exps}
        reviews = {d["id"]: [r for r in tx.all("Ticket", target_id=d["id"]) if r.get("review_kind") == "conclusion"]
                   for d in decisions}
        asks = tx.all("Ask")
        findings_open = {r["id"]: len(open_findings(r)) for r in tx.all("Ticket") if r["kind"] == "review"}
    return locals()


def _iso_ms(at: str | None) -> str:
    return (at or now_utc())


def draft_panel(store: Store) -> str:
    """The unrecorded tree draft (research/tree-draft/tree.yaml), in full and marked as a draft: the user reads it
    here before approving it (design 2026-11-27, 7.2). Empty when there is none."""
    if not tree.has_draft(store.project):
        return ""
    d = tree.read(store.project)
    rel = f"{tree.DRAFT_DIR}/{tree.DRAFT_FILE}"
    head = (f"<section class=\"panel draft\" aria-label=\"建树草稿\"><h2>建树草稿 <span class=\"badge\">草稿，还没有记录"
            f"</span></h2><p class=\"muted\"><span class=\"mono\">{esc(rel)}</span> · 你批准以后，协调者用 "
            "<span class=\"mono\">rl tree record</span> 一次记下；这之前要改哪里，告诉协调者，改多少次都行。</p>")
    problems = ("<div class=\"problems\"><b>格式问题（记录之前要改好）：</b><ul>"
                + "".join(f"<li>{esc(p)}</li>" for p in d.problems) + "</ul></div>") if d.problems else ""
    if not d.parsed:
        return head + problems + f"<pre class=\"gl\">{esc(d.raw)}</pre></section>"
    kind = {"main": "主假设", "competing": "竞争解释"}
    lack = {"novelty": "新颖性", "feasibility": "可行性"}
    hyps = []
    for h in d.hypotheses:
        rows = "".join(f"<dt>{name}</dt><dd>{esc(h.get(key))}</dd>" for name, key in (
            ("命题", "statement"), ("范围", "scope"), ("目的", "purpose"), ("出处", "source"), ("挂在", "parent"),
            ("预计用量", "cost")) if h.get(key))
        doubt = h.get("doubt")
        hyps.append(f"<article class=\"dh\"><h4><span class=\"mono\">{esc(h['key'])}</span> {esc(h.get('title'))} "
                    f"<span class=\"tag\">{esc(kind.get(h.get('kind') or '', h.get('kind') or '?'))}</span></h4>"
                    f"<dl>{rows}</dl>" + (f"<p class=\"doubt\"><b>存疑（{esc(lack.get(doubt.get('lack') or '', doubt.get('lack')))}"
                                          f"不足）</b>：{esc(doubt.get('basis'))}<br><span class=\"muted\">选项：</span>"
                                          f"{esc(doubt.get('options'))}</p>" if doubt else "") + "</article>")
    prep = "".join(f"<li><b>{esc(p.get('title'))}</b>：{esc(p.get('requirement'))}<br><span class=\"muted\">完成检查："
                   f"</span><code>{esc(p.get('done_check'))}</code></li>" for p in d.prep)
    questions = "".join(f"<li>{esc(q)}</li>" for q in d.questions)
    return (head + problems + f"<h3>为什么是这几个假设</h3><p>{esc(d.why or '（没写）')}</p>"
            f"<h3>假设（{len(d.hypotheses)}）</h3>{''.join(hyps) or '<p>没有。</p>'}"
            f"<h3>共用的准备工作</h3><ul>{prep or '<li>没有。</li>'}</ul>"
            f"<h3>要问你的事</h3><ul>{questions or '<li>没有。</li>'}</ul></section>")


def render(store: Store) -> str:
    c = collect(store)
    hyps, exps, tasks = c["hyps"], c["exps"], c["tasks"]
    by_id = {t["id"]: t for t in tasks}
    now = datetime.now(timezone.utc)
    every = agents_value(store.project.config, "page_minutes")
    draft = draft_panel(store)

    # -- tree data for page-main.js ------------------------------------------------------------------------------
    def item(hid: str) -> list:
        sub = [[e, []] for e, ts in c["tests"].items() if hid in ts]
        return [hid, sub + [item(k) for k in c["kids"].get(hid, [])]]
    roots = [h for h in hyps if not c["parents"].get(h)]
    lines = [{"key": f"l{i}", "name": f"{hyps[r].get('label')} {hyps[r]['title']}",
              "desc": hyps[r].get("purpose") or "", "tree": [item(r)]} for i, r in enumerate(sorted(roots))]
    nodes = {}
    final = {d["id"]: d for d in c["decisions"] if d["state"] == "final"}
    for hid, h in hyps.items():
        dec = next((d for d in reversed(list(final.values())) if hid in c["decided"].get(d["id"], [])
                    and d["kind"] in ("verdict", "stop")), None)
        cost = "; ".join(f"{c['usage'][t['id']]}" for t in tasks if t["kind"] == "hypothesis"
                         and hid in c["targets"][t["id"]])
        nodes[hid] = {"kind": "H", "title": f"{h.get('label')} {h['title']}", "purpose": h.get("purpose") or "",
                      "did": (dec or {}).get("did") or "还没有结论。",
                      "result": ((dec or {}).get("means") or "") + (
                          f"（{dec['id']}：{dec.get('evidence_status_set') or dec.get('disposition_set')}）" if dec
                          else "") + (f" 花费 {cost}" if cost else ""),
                      "ev": h["evidence_status"], "disp": h["disposition"],
                      "derived": [c["parents"][hid]] if c["parents"].get(hid) else []}
    for eid, e in exps.items():
        ts = c["exp_task"].get(eid, [])
        done = [t for t in ts if t["state"] == "accepted"]
        nodes[eid] = {"kind": "E", "title": f"{e.get('label')} {e['title']}", "purpose": e.get("question") or "",
                      "did": "；".join(f"{t['id']} {t['state']}" for t in ts) or "还没有任务。",
                      "result": "；".join(f"{t.get('conclusion') or ''}" + (f"（限制：{t['limitations']}）"
                                                                             if t.get("limitations") else "")
                                         for t in done) or "还没有验收的结果。",
                      "stage": "", "run": "running" if c["running"][eid] else "done" if done else "notstarted",
                      "tests": c["tests"].get(eid, [])}
    data = {"lines": lines, "nodes": nodes, "meta": {"data_version": c["meta"].get("data_version"),
                                                     "generated": now_utc()}}
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    data_json = data_json.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")

    # -- now ---------------------------------------------------------------------------------------------------
    waits: dict[str, inbox.Item] = {}
    for it in c["items"]:
        waits.setdefault(it.subject, it)
    now_rows = []
    for t in tasks:
        if t["kind"] != "hypothesis" or t["state"] in TASK_DONE:
            continue
        target = ", ".join(c["targets"][t["id"]])
        kid_items = [i for i in c["items"] if i.subject == t["id"] or c["part"].get(i.subject) == t["id"]]
        who = kid_items[0] if kid_items else None
        overdue = any(i.type == "timeout" and i.subject == t["id"] for i in c["items"])
        now_rows.append(
            f"<tr{' class=\"late\"' if overdue else ''}><td class=\"mono\">{esc(t['id'])}</td><td>{esc(target)}</td>"
            f"<td>{esc(c['steps'].get(t['id']))}</td><td>{esc(who.to if who else '等别人或外部事件')}</td>"
            f"<td class=\"since\" data-since=\"{esc(_iso_ms(who.since if who else t.get('released_at')))}\"></td>"
            f"<td>{esc(c['usage'].get(t['id']))}</td></tr>")
    for t in tasks:
        if t["kind"] in ("tree", "phase") and t["state"] not in TASK_DONE:
            who = next((i for i in c["items"] if i.subject == t["id"]), None)
            now_rows.append(f"<tr><td class=\"mono\">{esc(t['id'])}</td><td>{esc(t['kind'])}</td>"
                            f"<td>{esc(t['state'])}</td><td>{esc(who.to if who else t.get('holder') or '-')}</td>"
                            f"<td class=\"since\" data-since=\"{esc(_iso_ms(who.since if who else None))}\"></td>"
                            "<td></td></tr>")
    day_ago = (now - timedelta(hours=24)).isoformat()
    shown = ("task.dispatched", "review.answered", "run.ended", "task.delivered", "task.accepted", "decision",
             "task.released", "task.merged")
    recent = [e for e in c["events"] if e["created_at"] >= day_ago and e["type"] in shown][-30:]
    recent_html = "".join(f"<li><span class=\"mono\">{esc(e['created_at'][5:16])}</span> {esc(e['type'])} "
                          f"<span class=\"mono\">{esc(e.get('entity_id') or '')}</span> {esc(e.get('summary') or '')}"
                          "</li>" for e in reversed(recent)) or "<li>最近 24 小时没有事。</li>"

    # -- findings ----------------------------------------------------------------------------------------------
    phase_html = []
    for ph in c["phases"]:
        pd = [d for d in c["decisions"] if d["kind"] == "phase" and ph["id"] in c["decided"].get(d["id"], [])]
        if not pd:
            continue
        d = pd[-1]
        hl = "".join(f"<li>{esc(x.get('decision'))}：{esc(x.get('why'))}</li>" for x in d.get("highlights") or [])
        cost = "；".join(c["usage"].get(t["id"], "") for t in tasks if c["in_phase"].get(t["id"]) == ph["id"]
                        and t["kind"] == "hypothesis")
        phase_html.append(f"<section class=\"phase\"><h3>{esc(ph['id'])} {esc(ph['title'])}</h3>"
                          f"<p>{esc(d['rationale'])}</p>" + (f"<ul>{hl}</ul>" if hl else "")
                          + f"<p class=\"muted\">花费：{esc(cost or '—')}"
                          + (f"；预算（只显示）{esc(ph['budget'])}" if ph.get("budget") else "") + "</p></section>")
    concl = []
    unsure = 0
    for d in c["decisions"]:
        if d["kind"] != "verdict" or d["state"] != "final":
            continue
        if d.get("evidence_status_set") == "inconclusive":
            unsure += 1
            continue
        hs = ", ".join(c["decided"].get(d["id"], []))
        t = by_id.get(d.get("task_id") or "")
        concl.append(f"<li><b>{esc(hs)}</b> {esc(d.get('evidence_status_set'))} · {esc((t or {}).get('conclusion'))}"
                     f"<br><span class=\"muted\">做了什么：</span>{esc(d.get('did'))}"
                     f"<br><span class=\"muted\">意味着：</span>{esc(d.get('means'))}"
                     f"<br><span class=\"muted mono\">{esc(d['id'])} · 花费 {esc(c['usage'].get(d.get('task_id'), '—'))}"
                     "</span></li>")
    interim = []
    for t in tasks:
        parent = by_id.get(c["part"].get(t["id"]) or "")
        if t["kind"] == "execute" and t["state"] == "accepted" and parent and parent["state"] not in TASK_DONE:
            interim.append(f"<li><span class=\"mono\">{esc(t['id'])}</span>（{esc(parent['id'])}）"
                           f"{esc(t.get('conclusion'))}" + (f"；限制：{esc(t['limitations'])}" if t.get("limitations")
                                                           else "") + "</li>")

    # -- waiting on you ----------------------------------------------------------------------------------------
    mine = [i for i in c["items"] if i.to == "user"]
    ask_by = {a.get("task_id"): a for a in c["asks"]}
    ask_by.update({a["id"]: a for a in c["asks"]})
    you = []
    for i in mine:
        a = ask_by.get(i.subject) or {}
        you.append(f"<li><b>{esc(i.type)}</b> <span class=\"mono\">{esc(i.subject)}</span>：{esc(i.ask)}"
                   + (f"<br>{esc(a.get('stuck'))}" if a.get("stuck") else "")
                   + (f"<br><span class=\"muted\">选项：</span>{esc(a.get('options'))}" if a.get("options") else "")
                   + (f"<br><span class=\"muted\">建议：</span>{esc(a.get('recommend'))}" if a.get("recommend") else "")
                   + f" <span class=\"since muted\" data-since=\"{esc(i.since)}\"></span></li>")

    glossary = store.project.root / "docs" / "glossary.md"
    gloss = ""
    if glossary.is_file():
        gloss = ("<details class=\"glossary\"><summary>术语表</summary><pre class=\"gl\">"
                 + esc(glossary.read_text(encoding="utf-8")) + "</pre></details>")
    title = store.project.config.get("title") or store.project.root.name
    hs = list(hyps.values())
    cnt = {k: sum(1 for h in hs if h["evidence_status"] == k) for k in ("supported", "challenged", "inconclusive",
                                                                          "untested")}
    lede = (f"{len(hs)} 个假设、{len(exps)} 个实验。支持 {cnt['supported']}，受挑战 {cnt['challenged']}，"
            f"无定论 {cnt['inconclusive']}，未检验 {cnt['untested']}。")
    script = """
(function(){
  var gen = Date.parse(document.body.getAttribute('data-generated'));
  var every = parseFloat(document.body.getAttribute('data-every')) * 60000;
  function fmt(ms){ var m = ms/60000; if (m < 1) return '刚刚'; if (m < 90) return Math.round(m)+' 分钟';
    if (m < 2880) return (m/60).toFixed(1)+' 小时'; return (m/1440).toFixed(1)+' 天'; }
  function tick(){
    var now = Date.now();
    document.querySelectorAll('[data-since]').forEach(function(el){
      var t = Date.parse(el.getAttribute('data-since')); if (!isNaN(t)) el.textContent = '等了 ' + fmt(now - t); });
    var warn = document.getElementById('stale');
    if (warn) warn.hidden = !(now - gen > 2 * every);
  }
  tick(); setInterval(tick, 60000);
  setTimeout(function(){ location.reload(); }, every);
})();
"""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)} · 研究页</title>
<script>try{{var t=localStorage.getItem('ht-theme');if(t==='light'||t==='dark'){{document.documentElement.setAttribute('data-theme',t);}}}}catch(e){{}}</script>
<style>{_asset('page.css')}
.panel{{margin:1.2rem 0;padding:1rem 1.2rem;border:1px solid var(--line, #8884);border-radius:10px}}
.panel table{{width:100%;border-collapse:collapse;font-size:.92rem}}
.panel td,.panel th{{padding:.3rem .4rem;border-bottom:1px solid var(--line, #8883);text-align:left;vertical-align:top}}
.panel tr.late td{{color:#c33}} .muted{{opacity:.7}} .mono{{font-family:ui-monospace,monospace}}
#stale{{background:#8883;padding:.5rem 1rem;border-radius:8px}} pre.gl{{white-space:pre-wrap}}
.draft{{border:2px dashed #c80}} .draft .badge{{background:#c80;color:#fff;font-size:.8rem;padding:.1rem .5rem;border-radius:6px;vertical-align:middle}}
.draft .dh{{margin:.8rem 0;padding:.6rem .8rem;border-left:3px solid #c808}} .draft .dh h4{{margin:.2rem 0 .4rem}}
.draft dl{{display:grid;grid-template-columns:max-content 1fr;gap:.2rem .8rem;margin:0}} .draft dt{{opacity:.7}} .draft dd{{margin:0}}
.draft .tag{{font-size:.8rem;border:1px solid currentColor;border-radius:6px;padding:0 .4rem;opacity:.8}}
.draft .doubt{{color:#b60}} .draft .problems{{color:#c33}}
</style>
</head>
<body data-generated="{esc(now_utc())}" data-every="{every:g}">
<header class="top"><div class="wrap">
<p class="kicker">smooth-research 研究页 · 生成于 {esc(now_utc()[:16])} UTC</p>
<h1>{esc(title)}</h1>
<p class="lede">{esc(lede)}</p>
<p id="stale" hidden>本页生成于 {esc(now_utc()[:16])} UTC；运行 rl view 刷新。</p>
</div></header>
<main class="wrap">
{draft}
<section class="panel" aria-label="现在"><h2>现在</h2>
<table><thead><tr><th>任务</th><th>目标</th><th>走到哪一步</th><th>在等谁</th><th>等了多久</th><th>花费/信封</th></tr></thead>
<tbody>{''.join(now_rows) or '<tr><td colspan="6">没有在做的假设任务。</td></tr>'}</tbody></table>{'<p class="muted">花费后面带 * 的：其中有的用量记录同时覆盖别的任务的运行，在每个任务里都按全额算，几个任务的花费不能相加。</p>' if any(str(v).split('/')[0].endswith('*') for v in c['usage'].values()) else ''}
<h3>最近 24 小时</h3><ul>{recent_html}</ul></section>
<section class="panel" aria-label="等你决定"><h2>等你决定</h2>
<ul>{''.join(you) or '<li>没有等你决定的事。</li>'}</ul>
<p class="muted">在协调者的面板里答复。</p></section>
<section class="panel" aria-label="发现"><h2>发现</h2>
<h3>阶段结论</h3>{''.join(phase_html) or '<p>还没有阶段结论。</p>'}
<h3>通过结论审核的假设结论</h3><ul>{''.join(concl) or '<li>还没有。</li>'}</ul>
{f'<p class="muted">另有 {unsure} 个结论为“不确定”（折叠）。</p>' if unsure else ''}
<h3>中间结果，还没下结论</h3><ul>{''.join(interim) or '<li>没有。</li>'}</ul></section>
<section aria-label="树和花费"><h2>树和花费</h2>
<div class="toolbar">
<button type="button" class="btn" id="expand-all">全部展开</button>
<button type="button" class="btn" id="collapse-all">全部折叠</button>
<label class="filter"><span>筛选</span><input id="filter" type="search" placeholder="编号或标题" autocomplete="off"></label>
<button type="button" class="btn" id="theme">配色：自动</button>
</div>
<figure class="overview"><div id="ov-grid"></div></figure>
<div class="cols">
<nav aria-label="假设树"><ul class="tree" id="tree"></ul><p class="no-match" id="no-match" hidden>没有匹配的条目。</p></nav>
<aside class="detail" id="detail" aria-label="详情">
<p class="sr" id="sr-status" aria-live="polite"></p>
<p class="hint" id="detail-empty">点击左侧任一条目，这里显示它的目的、做了什么与结果。</p>
<article id="detail-card" hidden>
<header><p class="d-id" id="d-id"></p><h2 class="d-title" id="d-title" tabindex="-1"></h2>
<div class="d-badges" id="d-badges"></div><p class="d-rel" id="d-rel"></p></header>
<section><h3>目的</h3><p id="d-purpose"></p></section>
<section><h3>做了什么</h3><p id="d-did"></p></section>
<section><h3>结果</h3><p id="d-result"></p></section>
<button type="button" class="btn back" id="back-to-tree">回到树</button>
</article></aside></div></section>
{gloss}
</main>
<footer class="wrap foot"><p>由记录生成（data_version {esc(c['meta'].get('data_version'))}）。不要手改：rl view（或观察者）会重画。</p></footer>
<script type="application/json" id="tree-data">{data_json}</script>
<script>{_asset('page-main.js')}</script>
<script>{script}</script>
</body>
</html>
"""


def write(store: Store) -> dict:
    text = render(store)
    out = store.project.root / "docs" / PAGE
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(out, text.encode("utf-8"))
    return {"path": str(out), "summary": f"wrote docs/{PAGE} ({len(text) // 1024} KB)"}


def _age(at: str) -> float:
    return (datetime.now(timezone.utc) - parse_time(at)).total_seconds() / 60
