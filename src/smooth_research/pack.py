"""Packs: the one screen a new conversation reads, put together from the records when it is read (design 11.5).

Every pack has the same five blocks, cut to a budget in this order: the role card; the card of the thing this
session is for; its inbox; the necessary material (one line per record, the oldest cut first, with a count of what
was cut); what else can be fetched by id. Nothing here is stored.
"""

from __future__ import annotations

from . import documents, gitops, inbox, refs
from .cards import card
from .research import brief, task_experiment, tree_lines
from .reviews import blocking, open_findings, task_rounds
from .roles import Actor, parse
from .schema import TASK_DONE
from .store import Store, Tx
from .tasks import phase_tasks, schedule_lines, work_dir
from .usage_view import task_usage_short, usage_line
from .util import RLError, now_utc
from .verify import hypothesis_lines, task_lines

BUDGETS = {"coordinator": 6000, "scientist": 12000, "researcher": 7000, "reviewer": 6000, "advisor": 6000}


def _section(title: str, lines: list[str]) -> str:
    return f"## {title}\n" + ("\n".join(lines) if lines else "(none)")


def _fit(blocks: list[tuple[str, list[str], bool]], budget: int) -> str:
    """Blocks are (title, lines, cuttable); cuttable ones lose their oldest (first) lines until the pack fits."""
    def size() -> int:
        return sum(len(t) + sum(len(x) + 1 for x in ls) for t, ls, _ in blocks)
    blocks = [(t, list(ls), c) for t, ls, c in blocks]
    cut = {t: 0 for t, _, _ in blocks}
    while size() > budget:
        victims = [(t, ls) for t, ls, c in blocks if c and len(ls) > 1]
        if not victims:
            break
        t, ls = max(victims, key=lambda v: sum(len(x) for x in v[1]))
        ls.pop(0)
        cut[t] += 1
    out = []
    for t, ls, _ in blocks:
        if cut[t]:
            ls = [f"({cut[t]} older line(s) cut; rl show / rl history for them)"] + ls
        out.append(_section(t, ls))
    return "\n\n".join(out)


def _doc_text(store: Store, tx: Tx, ref: str | None, limit_chars: int = 4000) -> list[str]:
    if not ref:
        return []
    try:
        rev = documents.resolve(tx, ref)
        text = documents.read(store.project, rev)
    except RLError as exc:
        return [f"{ref}: unreadable ({exc.message})"]
    if len(text) > limit_chars:
        text = text[:limit_chars] + f"\n… (cut; rl show {ref} prints it all)"
    return [f"{ref} ({rev['source_path']}):", text]


def _inbox(tx: Tx, store: Store, who: str) -> list[str]:
    items = inbox.for_recipient(tx, store.project.config, who)
    return inbox.render(items, who).splitlines()[1:] or ["nothing waits on you: end your turn"]


def build(store: Store, actor: Actor) -> dict:
    role = actor.role
    with store.read() as tx:
        if role == "coordinator":
            body = _coordinator(store, tx)
        elif role == "scientist":
            body = _scientist(store, tx, actor)
        elif role == "researcher":
            body = _researcher(store, tx, actor)
        elif role == "reviewer":
            body = _reviewer(store, tx, actor)
        elif role == "advisor":
            body = _advisor(store, tx, actor)
        else:
            raise RLError("INVALID", f"no pack for {actor.name}")
        blocks = body + [("Your inbox", _inbox(tx, store, actor.name), False)]
    head = f"# Pack · {actor.name} · {now_utc()[:16]} UTC\nRead from the records only; nothing was queried."
    text = head + "\n\n" + card(role) + "\n\n" + _fit(blocks, BUDGETS[role])
    return {"pack": text, "summary": text}


# -- coordinator ----------------------------------------------------------------------------------------------


def _coordinator(store: Store, tx: Tx) -> list[tuple[str, list[str], bool]]:
    plan = store.project.plan_path
    title = ""
    if plan.is_file():
        first = [x for x in plan.read_text(encoding="utf-8").splitlines() if x.strip()][:1]
        title = first[0].lstrip("# ").strip() if first else ""
    constraints = store.project.root / "docs" / "constraints.md"
    cons = constraints.read_text(encoding="utf-8").splitlines()[:25] if constraints.is_file() else []
    project = [f"plan: {store.project.rel(plan)}" + (f" — {title}" if title else "")] + cons
    phases = []
    for ph in tx.all("Phase"):
        ts = phase_tasks(tx, ph["id"])
        done = len([t for t in ts if t["state"] in TASK_DONE])
        phases.append(f"{ph['id']} {ph['title']} · {done}/{len(ts)} done · scientists ≤{ph.get('scientist_limit')}"
                      + (f" · budget (shown only) {ph['budget']}" if ph.get("budget") else ""))
    trees = [t for t in tx.all("Task", kind="tree")]
    other = [f"{t['id']} tree/{t['state']}" + (f" ({t['holder']})" if t.get("holder") else "") for t in trees]
    other += [f"{t['id']} {t['kind']}/{t['state']} {t['title']}" for t in tx.all("Task")
              if t["kind"] in ("phase", "prep") and t["creator"] == "coordinator" and t["state"] not in TASK_DONE]
    cands = [brief(h) + (f" (not now: {h['defer_reason']})" if h.get("defer_reason") else "")
             for h in tx.all("Hypothesis")
             if h["evidence_status"] == "untested" and (h["disposition"] in ("candidate", "paused")
                                                         or h.get("defer_reason"))]
    recent = sorted([t for t in tx.all("Task", kind="hypothesis") if t["state"] == "accepted"],
                    key=lambda t: t.get("updated_at") or "")[-2:]
    received = [x for t in recent for x in [f"{t['id']}: {t.get('conclusion') or ''}"] + hypothesis_lines(tx, t)]
    return [("Project: goal and constraints", project, False),
            ("Phases", phases, True),
            ("Schedule: hypothesis tasks", schedule_lines(tx) or ["no hypothesis task yet"], True),
            ("Tree, phase and public tasks", other, True),
            ("Candidate and paused hypotheses", cands, True),
            ("Last received hypothesis tasks", received, True),
            ("Fetch by id", ["rl show T-x|H-x|D-x|RQ-x · rl tree · rl inbox --item N · rl status",
                             "Not in this pack: the plan's full text (read it once at the start), code, logs, diffs. "
                             "To explain details to the user, have a read-only subagent read them."], False)]


# -- scientist ------------------------------------------------------------------------------------------------


def _task_card(tx: Tx, t: dict) -> list[str]:
    lines = [f"{t['id']} {t['kind']} · {t['state']}" + (f" · held by {t['holder']}" if t.get("holder") else "")
             + f" · {t['title']}"]
    for key in ("reason", "priority", "envelope", "parallel_limit", "requirement", "done_check", "estimate",
                "block_reason", "block_note", "answer_note", "return_note", "dispatch_note", "merge_state",
                "merge_note", "worktree", "branch"):
        if t.get(key) not in (None, ""):
            lines.append(f"{key}: {t[key]}")
    return lines


def _scientist(store: Store, tx: Tx, actor: Actor) -> list[tuple[str, list[str], bool]]:
    t = tx.require(actor.subject, "Task")
    fetch = ("Fetch by id", ["rl show EV-x|RV-x|AD-x|D-x · rl task check T-x · rl paper search ..",
                             f"Your documents go in {work_dir(t['id'])}/ in your worktree; commit them."], False)
    if t["kind"] == "tree":
        plan = store.project.plan_path
        text = plan.read_text(encoding="utf-8").splitlines() if plan.is_file() else ["(no plan file)"]
        current = tree_lines(tx) or ["(no hypotheses yet)"]
        return [("Tree task card", _task_card(tx, t) + [
                    "Deliver: hypotheses (statement, scope, source, purpose; one primary parent each), a route "
                    "decision on why the tree is split this way, every plan heading covered (or in left-out.md), "
                    "flagged hypotheses with basis and options, a passed tree review."], False),
                ("The plan (full text)", text, False),
                ("Current tree", current, True), fetch]
    if t["kind"] == "phase":
        ph = next((tx.require(e["id"]) for e in tx.out(t["id"], "targets") if e["label"] == "Phase"), None)
        rows = []
        for ht in phase_tasks(tx, ph["id"]) if ph else []:
            rows.append(f"{ht['id']} {ht['kind']}/{ht['state']} {task_usage_short(tx, ht)} {ht.get('conclusion') or ''}")
            for d in tx.all("Decision", task_id=ht["id"]):
                if d["kind"] in ("verdict", "stop"):
                    rows.append("  " + brief(d))
        return [("Phase task card", _task_card(tx, t) + ([f"phase {ph['id']}: {ph['title']}"] if ph else []), False),
                ("The phase's tasks, conclusions and decisions", rows, True),
                ("Hypothesis tree", tree_lines(tx), True), fetch]
    if t["kind"] == "prep":
        atts = [f"{a['id']} {a['execution_state']}/{a['outcome']}" for r in tx.all("Run", task_id=t["id"])
                for a in tx.all("Attempt", run_id=r["id"])]
        rounds = [f"{r['id']} {r['status']} · {len(open_findings(r))} open" for r in task_rounds(tx, t["id"])]
        return [("Preparation task card (handed to you)", _task_card(tx, t), False),
                ("Attempts", atts, True), ("Review rounds", rounds, True), fetch]
    # hypothesis task
    hyps = [tx.require(e["id"]) for e in tx.out(t["id"], "targets") if e["label"] == "Hypothesis"]
    cardl = _task_card(tx, t)
    for h in hyps:
        cardl += [f"{h['id']} {h.get('label')}: {h['statement']}", f"  scope: {h.get('scope') or '-'} · purpose: "
                  f"{h.get('purpose') or '-'}" + (" · composite" if h.get("composite") else "")]
    mine = {h["id"] for h in hyps}
    mark = {}
    for h in hyps:
        mark[h["id"]] = "yours"
        for e in tx.out(h["id"], "assumes"):
            mark[e["id"]] = f"premise of {h['id']}"
        for e in tx.inc(h["id"], "assumes"):
            mark[e["id"]] = f"assumes {h['id']}"
    chain = []
    for h in hyps:
        cur = h["id"]
        while cur:
            chain.append(cur)
            cur = next((e["id"] for e in tx.out(cur, "derived_from") if e.get("primary")), None)
    decs = []
    for hid in dict.fromkeys(chain):
        for e in tx.inc(hid, "decides"):
            decs.append(brief(tx.require(e["id"])))
    for h in hyps:
        if h.get("composite"):
            for e in tx.inc(h["id"], "derived_from"):
                if e.get("primary"):
                    for d in tx.inc(e["id"], "decides"):
                        decs.append("child " + brief(tx.require(d["id"])))
    work = []
    for e in tx.inc(t["id"], "part_of"):
        k = tx.require(e["id"])
        work.append(f"{k['id']} {k['kind']}/{k['state']}" + (f" ({k['holder']})" if k.get("holder") else "")
                    + f" {k['title']}")
        exp = task_experiment(tx, k["id"]) if k["kind"] == "execute" else None
        if exp:
            work.append("  " + brief(exp))
    exps = [brief(tx.require(e["id"])) for hid in mine for e in tx.inc(hid, "tests")]
    reusable = [f"{o['path']} — {o['reusable']} ({k['id']})" for k in tx.all("Task") if k["state"] == "accepted"
                for o in k.get("outputs") or [] if isinstance(o, dict) and o.get("reusable")]
    return [("Hypothesis task card", cardl, False),
            ("Hypothesis tree (one line each)", tree_lines(tx, mark), True),
            ("Decisions on this hypothesis and its ancestors", decs, True),
            ("Experiments and tasks under it", exps + work, True),
            ("Reusable outputs", reusable, True), fetch]


# -- researcher -----------------------------------------------------------------------------------------------


def _researcher(store: Store, tx: Tx, actor: Actor) -> list[tuple[str, list[str], bool]]:
    t = tx.require(actor.subject, "Task")
    cardl = _task_card(tx, t)
    exp = task_experiment(tx, t["id"]) if t["kind"] == "execute" else None
    if exp:
        cardl += [f"experiment {exp['id']}@{exp['revision']}: {exp['question']}",
                  f"design commit: {exp.get('design_commit') or 'not sealed'}",
                  f"manifest: {exp.get('manifest_path')}  · stop_when: {(exp.get('manifest') or {}).get('stop_when')}",
                  "arms: " + "; ".join(f"{a.get('name')} seeds {a.get('seeds')} split {a.get('split')} config "
                                        f"{a.get('config')}" for a in (exp.get("manifest") or {}).get("arms") or []),
                  f"protected (do not change): {', '.join(exp.get('protected') or []) or '-'}",
                  f"semantic tests: {exp.get('semantic_tests') or '-'}"]
        cardl += _doc_text(store, tx, exp.get("brief_ref"), 2500)
    cardl += _doc_text(store, tx, t.get("mandate_ref"), 2500)
    running = [f"{a['id']} {a['execution_state']} until {(a.get('expected_end') or '?')[11:16]} (run {r['id']} "
               f"{r['category']} arm {r.get('arm')} seed {r.get('seed')})"
               for r in tx.all("Run", task_id=t["id"]) for a in tx.all("Attempt", run_id=r["id"])
               if a["execution_state"] != "ended"]
    ended = [f"{a['id']} ended {a['outcome']} result {a['result_status']}" for r in tx.all("Run", task_id=t["id"])
             for a in tx.all("Attempt", run_id=r["id"]) if a["execution_state"] == "ended"]
    rounds = []
    for r in task_rounds(tx, t["id"]):
        if r["status"] in ("passed", "closed"):
            continue
        rounds.append(f"{r['id']} {r['status']} · segment {r.get('segment')} · passes {len(r.get('passes') or [])}/3")
        rounds += [f"  F{f['n']} {f['category']}/{f['severity']}/{f['status']}: {f['text'][:200]}"
                   for f in r.get("findings") or [] if f["status"] in ("open", "draft")]
    deps = [f"{d['id']} {tx.require(d['id'])['title']}: " + ", ".join(
        o["path"] if isinstance(o, dict) else o for o in tx.require(d["id"]).get("outputs") or [])
        for d in tx.out(t["id"], "depends_on")]
    from .project import compute_default
    compute = [f"default resource: {compute_default(store.project.config) or 'local'} (rl compute steps for a cluster)"]
    return [("Task card", cardl, False),
            ("Jobs still running (check them first)", running, False),
            ("Ended attempts", ended, True),
            ("Open review rounds and findings", rounds, False),
            ("Outputs you depend on", deps, True),
            ("Compute", compute, False),
            ("Fetch by id", ["rl show RV-x|EV-x|RES-x · rl run show ATT-x · rl paper search ..",
                             "Not in this pack: the hypothesis tree, other tasks, other roles' messages."], False)]


# -- reviewer -------------------------------------------------------------------------------------------------


def _reviewer(store: Store, tx: Tx, actor: Actor) -> list[tuple[str, list[str], bool]]:
    r = tx.require(actor.subject, "Ticket")
    lp = (r.get("passes") or [{}])[-1]
    cardl = [f"{r['id']} {r.get('review_kind')} review · {r['status']} · pass {len(r.get('passes') or [])}"
             + (f" · segment {r['segment']}" if r.get("segment") else ""),
             f"target {r.get('target_id')} · task {r.get('task_id')} · requested by {lp.get('requested_by')}",
             f"question: {lp.get('question') or '-'}"]
    material: list[str] = []
    kind = r.get("review_kind")
    if kind == "design":
        t = tx.require(r["task_id"], "Task")
        exp = task_experiment(tx, t["id"])
        commit = lp.get("commit")
        material += [f"commit under review: {commit}", f"worktree: {t.get('worktree')} (read with git show "
                     f"{(commit or '')[:12]}:<path>)"]
        if exp:
            material += [f"experiment {exp['id']}@{exp['revision']}: {exp['question']}",
                         f"manifest: {exp.get('manifest_path')}", f"protected: {', '.join(exp.get('protected') or [])}",
                         f"semantic tests: {exp.get('semantic_tests')}"]
            material += _doc_text(store, tx, exp.get("brief_ref"), 2500)
        if len(r.get("passes") or []) > 1:
            prev = r["passes"][-2].get("commit")
            material += [f"recheck: diff with `git diff {(prev or '')[:12]} {(commit or '')[:12]}`"]
        tests = [x for x in t.get("tests") or [] if x["commit"] == commit]
        material += [f"tests at this commit: {', '.join(x['outcome'] for x in tests) or 'none recorded'}"]
    elif kind == "conclusion":
        d = tx.require(r["target_id"], "Decision")
        material += [brief(d), f"did: {d.get('did')}", f"means: {d.get('means')}",
                     f"supports: {d.get('supports')} · challenges: {d.get('challenges')}"]
        for e in tx.out(d["id"], "based_on"):
            material.append("cites " + brief(tx.require(e["id"])))
        for ev in (d.get("supports") or []) + (d.get("challenges") or []):
            material.append("evidence " + brief(tx.require(ev)))
        task = tx.get(d.get("task_id") or "")
        if task:
            material += hypothesis_lines(tx, task)
            for e in tx.inc(task["id"], "part_of"):
                exp = task_experiment(tx, e["id"])
                if exp:
                    material.append(f"sealed design {exp['id']}@{exp['revision']} at "
                                    f"{(exp.get('design_commit') or '-')[:12]}: {exp['question']}")
    elif kind == "tree":
        material += tree_lines(tx)
        for h in tx.all("Hypothesis"):
            if h.get("origin") == r["task_id"]:
                material.append(f"{h['id']} statement: {h['statement']} · source: {h.get('source')} · scope: "
                                f"{h.get('scope')} · purpose: "
                                f"{h.get('purpose')}" + (f" · FLAG {h['flag']}" if h.get("flag") else ""))
        t = tx.require(r["task_id"])
        left = gitops.checkout_in(store.project, t["id"]) / work_dir(t["id"]) / "left-out.md"
        material += ["left out of the tree:"] + (left.read_text(encoding="utf-8").splitlines()[:40]
                                                 if left.is_file() else ["(nothing listed)"])
        material += [f"the plan: {store.project.rel(store.project.plan_path)} (read it whole)"]
    findings = [f"F{f['n']} {f['category']}/{f['severity']}/{f['status']}: {f['text'][:200]}"
                for f in r.get("findings") or []]
    from .messages import thread_lines
    return [("Review card", cardl, False), ("Material", material, True),
            ("Findings so far", findings, False), ("Discussion", thread_lines(tx, r["id"]), True),
            ("Fetch by id", ["rl show EV-x|RES-x|D-x · git show <commit>:<path>",
                             "Not in this pack: project status, the authors' other messages."], False)]


# -- advisor --------------------------------------------------------------------------------------------------


def _advisor(store: Store, tx: Tx, actor: Actor) -> list[tuple[str, list[str], bool]]:
    a = tx.require(actor.subject, "Ticket")
    cardl = [f"{a['id']} {a.get('advice_kind')} · {a['status']} · asked by {a.get('requested_by')}",
             f"question: {a.get('question')}"]
    material: list[str] = []
    if a.get("advice_kind") == "ruling":
        rv = tx.require(a["ruling_of"], "Ticket")
        material += [f"round {rv['id']} ({rv.get('review_kind')}), blocking findings:"]
        material += [f"F{f['n']} {f['category']}/{f['severity']}: {f['text']}" for f in blocking(rv)]
        from .messages import thread_lines
        material += ["discussion:"] + thread_lines(tx, rv["id"])
    else:
        task = tx.get(a.get("task_id") or "")
        if task:
            for e in tx.out(task["id"], "targets"):
                material.append(brief(tx.require(e["id"])))
            for d in tx.all("Decision", task_id=task["id"]):
                material.append(brief(d))
            material.append(usage_line(tx, task))
    return [("Advice card", cardl, False), ("Material", material, True),
            ("Fetch by id", ["rl show H-x|EV-x|D-x · rl paper search ..", "Not in this pack: task flow, code."],
             False)]


def item(store: Store, who: str, n: int) -> dict:
    """`rl inbox --item N`: one item with its subject expanded, its check lines and the next commands (8.11)."""
    with store.read() as tx:
        items = inbox.for_recipient(tx, store.project.config, who)
        if not 1 <= n <= len(items):
            raise RLError("NOT_FOUND", f"{who} has {len(items)} item(s)")
        it = items[n - 1]
        lines = inbox.render([it], who).splitlines()[1:]
        lines += ["", "subject:"] + refs.expand(tx, store.project, [it.subject.split("#")[0]])
        rec = tx.get(it.subject.split("#")[0])
        if rec and rec["_label"] == "Task":
            if rec["kind"] in ("execute", "prep"):
                lines += ["", "check lines:"] + task_lines(tx, store.project, rec)
            elif rec["kind"] == "hypothesis":
                lines += ["", "check lines:"] + hypothesis_lines(tx, rec)
            if rec["kind"] == "tree" and it.to == "coordinator":
                lines += ["", "the tree:"] + tree_lines(tx)
        if rec and rec["_label"] == "Ask":
            lines += ["", f"stuck: {rec['stuck']}", f"options: {rec.get('options')}",
                      f"recommend: {rec.get('recommend')}", f"cost: {rec.get('cost') or '-'}"]
        if rec and rec["_label"] == "Ticket":
            lines += ["", "findings:"] + [f"F{f['n']} {f['category']}/{f['severity']}/{f['status']}: {f['text']}"
                                          for f in rec.get("findings") or []]
        lines += ["", f"next: {it.cmd}"]
    return {"item": it.__dict__, "summary": "\n".join(lines)}


def who_for(actor: Actor | None, explicit: str | None) -> Actor:
    if explicit:
        return parse(explicit)
    if actor is None:
        raise RLError("INVALID", "no session: run it in a session's pane, or name one with --for <session>")
    return actor
