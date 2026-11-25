"""`rl`: the single command-line entry point shared by every host and role.

Who calls is the session the command runs in (SMOOTH_RESEARCH_SESSION, set in every pane the watcher opens), or
--as <session> outside a pane. Output is a short summary with ids and pointers; --json prints the full result.
Errors print `error[CODE]: message` on stderr and exit 1 (3 for conflicts and stale revisions).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (compute, herdr, hooks, inbox, labels, launch, literature, messages, pack, page, refs, research,
               reviews, runs, snapshot, tasks, upgrade, usage, verify, watch)
from .project import find_project
from .roles import Actor, current, need
from .schema import ENUMS, LINKABLE, PAPER_ASSESSMENTS, TASK_DONE
from .store import Store
from .util import LABEL_RE, RLError, load_json_arg, load_json_records, now_utc, split_list

CONFLICT_CODES = {"CONFLICT", "STALE_REVISION"}
# arguments that may name hypotheses or experiments by label (H1.2): they become ids before the command runs
ID_ARGS = ("id", "targets", "hypotheses", "parent", "assumes", "based_on", "supports", "challenges", "experiment",
           "motivated_by", "target", "source", "on")


# what rl --read-only runs: the commands that read the records and write no file (None: every action)
READ_ONLY = {"status": None, "show": None, "history": None, "tree": None, "doctor": None, "inbox": None,
             "pack": None, "refs": None, "usage": None, "hypothesis": ("list",), "task": ("list",),
             "run": ("show",)}


def _check_read_only(args) -> None:
    """rl --read-only refuses up front a command that would write records, files or caches; the ones it runs open
    the records without upgrading them or creating anything (Store.read_only)."""
    actions = READ_ONLY.get(args.command, ())
    action = getattr(args, "action", None)
    if actions is not None and action not in actions:
        what = " ".join(x for x in (args.command, action) if x)
        raise RLError("READ_ONLY", f"rl {what} writes (records, files or caches), and --read-only writes nothing. "
                                   "It reads with status, show, history, tree, doctor, inbox, pack, refs and the "
                                   "list and show subcommands")


def _store(args) -> Store:
    read_only = getattr(args, "read_only", False)
    project = find_project(args.project, read_only=read_only)
    args.config_warnings = project.warnings
    args.store = Store(project, getattr(args, "as_", None) or "user", read_only=read_only)
    _resolve_labels(args, args.store)
    return args.store


def _resolve_labels(args, store: Store) -> None:
    def values(dest: str) -> list:
        value = getattr(args, dest, None)
        return [v for v in (value if isinstance(value, list) else [value]) if isinstance(v, str)]
    found = [d for d in ID_ARGS if any(LABEL_RE.match(p.strip()) for v in values(d) for p in v.split(","))]
    if not found:
        return
    with store.read() as tx:
        for dest in found:
            value = getattr(args, dest)
            items = [",".join(labels.resolve(tx, p.strip()) for p in v.split(","))
                     for v in (value if isinstance(value, list) else [value])]
            setattr(args, dest, items if isinstance(value, list) else items[0])


def _actor(args) -> Actor | None:
    return current(getattr(args, "as_", None))


def L(value) -> list[str]:
    return split_list(value)


# -- project --------------------------------------------------------------------------------------------------


def cmd_init(args):
    return research.init_project(Path(args.dir or "."), args.plan, args.title)


def cmd_pack(args):
    store = _store(args)
    return pack.build(store, pack.who_for(_actor(args), args.for_))


def cmd_inbox(args):
    store = _store(args)
    who = pack.who_for(_actor(args), args.for_).name if (args.for_ or _actor(args)) else "user"
    if args.item:
        return pack.item(store, who, args.item)
    with store.read() as tx:
        items = inbox.for_recipient(tx, store.project.config, who)
    return {"items": [i.__dict__ for i in items], "summary": inbox.render(items, who)}


def status_lines(store: Store) -> list[str]:
    with store.read() as tx:
        items = inbox.all_items(tx, store.project.config)
        lines = [f"project {store.project.root} · data_version {tx.meta().get('data_version')}"]
        lines.append("watcher: " + ("running" if watch.alive(store) else "NOT running (rl resume starts it)"))
        work = [t for t in tx.all("Task") if t["kind"] in ("tree", "hypothesis", "phase") and t["state"]
                not in TASK_DONE]
        lines.append("now:")
        for t in work:
            who = next((i for i in items if i.subject == t["id"]), None)
            lines.append(f"  {tasks.task_line(t)}" + (f" · waits on {who.to} ({inbox.age(inbox.minutes_since(who.since))})"
                                                      if who else ""))
        if not work:
            lines.append("  nothing at work")
        mine = [i for i in items if i.to == "user"]
        lines.append(f"waiting on you ({len(mine)}):")
        lines += [f"  {i.type} {i.subject}: {i.ask}" + (f" — {i.note}" if i.note else "") for i in mine]
        reports = [a for a in tx.all("Ask", kind="report")]
        since = reports[-1]["created_at"] if reports else ""
        new = [t for t in tx.all("Task", kind="hypothesis") if t["state"] == "accepted"
               and (t.get("delivered_at") or "") > since]
        if new:
            lines.append("hypothesis conclusions since the last phase report:")
            lines += [f"  {t['id']}: {t.get('conclusion')}" for t in new]
    return lines


def cmd_status(args):
    store = _store(args)
    return {"summary": "\n".join(status_lines(store))}


def cmd_resume(args):
    """Run the project on: the watcher and the coordinator's pane, then the status (8.11)."""
    store = _store(args)
    problem = herdr.problem()
    if problem:
        raise RLError("UNAVAILABLE", f"rl resume needs herdr: {problem}. Install herdr and run it inside a herdr pane")
    done = [watch.start(store), watch.ensure_coordinator(store)]
    return {"summary": "\n".join(done + [""] + status_lines(store))}


def cmd_view(args):
    return page.write(_store(args))


def cmd_doctor(args):
    return snapshot.doctor(_store(args))


def cmd_show(args):
    store = _store(args)
    out = research.show(store, args.id)
    with store.read() as tx:
        rec = out.get("record") or {}
        if rec.get("_label") == "Task" and rec["kind"] in ("execute", "prep"):
            try:
                lines = verify.task_lines(tx, store.project, rec)
            except RLError as exc:  # the records are readable without the repository (a restored snapshot)
                lines = [f"not available: {exc.message}"]
            out["summary"] += "\n\ncheck lines:\n" + "\n".join(lines)
        if rec.get("_label") == "Ticket":
            out["summary"] += "\n" + "\n".join(messages.thread_lines(tx, rec["id"]))
    return out


def cmd_history(args):
    return research.history(_store(args), args.id, args.limit)


def cmd_tree(args):
    store = _store(args)
    with store.read() as tx:
        return {"summary": "\n".join(research.tree_lines(tx)) or "no hypotheses yet"}


def cmd_link(args):
    return research.link(_store(args), args.source, args.relation, args.target, args.note, args.locator,
                         args.assessment)


def cmd_export(args):
    return snapshot.export(_store(args), args.out, args.records_only)


def cmd_restore(args):
    return snapshot.restore(args.snapshot, args.into)


def cmd_digest(args):
    return runs.digest_paths(args.paths)


def cmd_upgrade(args):
    store = _store(args)
    if args.move:
        return upgrade.move(store, *args.move)
    return upgrade.report(store, args.note)


def cmd_watch(args):
    store = _store(args)
    if args.action == "start":
        return {"summary": watch.start(store)}
    if args.action == "once":
        with watch.beat_lock(store) as mine:
            if not mine:
                return {"summary": "the watcher's own beat runs now"}
            return watch.beat(store)
    return watch.run(store)


def cmd_hook(args):
    return hooks.session_start(args.host, sys.stdin.read() if not sys.stdin.isatty() else "")


def cmd_usage(args):
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
    except (OSError, ValueError):
        raw = ""
    return {"summary": usage.statusline(raw, keep=not args.read_only)}


def cmd_compute(args):
    store = _store(args)
    if args.action == "steps":
        return compute.steps(store, args.resource, args.commit, args.attempt)
    if args.action == "sync":
        return compute.sync(store, args.resource)
    return compute.show(store, args.commit)


def cmd_docs(args):
    store = _store(args)
    return research.commit_user_docs(store, _actor(args), L(args.paths), args.message)


# -- records --------------------------------------------------------------------------------------------------


def cmd_hypothesis(args):
    store, actor, a = _store(args), _actor(args), args.action
    if a == "add":
        return research.add_hypothesis(store, actor, args.title, args.statement, args.purpose, args.scope,
                                       args.source, args.parent, L(args.assumes), args.cost, args.composite,
                                       motivated_by=L(args.motivated_by))
    if a == "revise":
        return research.revise_hypothesis(store, actor, args.id, args.reason, args.statement, args.scope, args.title,
                                          args.purpose, args.source, args.cost)
    if a == "flag":
        return research.flag_hypothesis(store, actor, args.id, args.lack, args.basis, args.options)
    if a in ("keep", "drop"):
        return research.settle_flag(store, actor, args.id, a)
    if a == "defer":
        return research.defer_hypothesis(store, actor, args.id, None if args.clear else args.reason)
    with store.read() as tx:
        return {"summary": "\n".join(research.tree_lines(tx)) or "no hypotheses yet"}


def cmd_experiment(args):
    store, actor = _store(args), _actor(args)
    if args.action == "add":
        return research.add_experiment(store, actor, args.title, args.question, L(args.hypotheses),
                                       args.motivated_by, args.manifest, L(args.protected), args.semantic_tests,
                                       args.brief, args.estimate)
    fields = {"manifest_path": args.manifest, "protected": L(args.protected) or None,
              "semantic_tests": args.semantic_tests, "brief": args.brief, "estimate": args.estimate,
              "title": args.title, "question": args.question}
    return research.update_experiment(store, actor, args.id, args.reason, **fields)


def cmd_evidence(args):
    store, actor = _store(args), _actor(args)
    if args.action == "add":
        return research.add_evidence(store, actor, args.task, args.summary, args.level, L(args.results),
                                     L(args.attempts), args.scope, L(args.artifacts), args.doc)
    return research.set_evidence(store, actor, args.id, args.validity, args.reason)


def cmd_decide(args):
    store, actor = _store(args), _actor(args)
    if args.final:
        return research.finalize(store, actor, args.final)
    if not args.kind or not args.rationale:
        raise RLError("INVALID", "rl decide --kind verdict|stop|route|phase --targets .. --rationale .. (or rl decide "
                                 "--final D-x)")
    highlights = load_json_arg(args.highlights) or []
    return research.decide(store, actor, args.kind, L(args.targets), args.rationale, L(args.based_on), args.status,
                           L(args.supports), L(args.challenges), args.did, args.means, highlights,
                           args.rationale_file)


def cmd_task(args):
    store, actor, a = _store(args), _actor(args), args.action
    if a == "create":
        return tasks.create(store, actor, args.kind, args.title, L(args.targets), args.priority, args.envelope,
                            args.parallel, args.reason, args.phase, args.experiment, args.requirement,
                            args.done_check, args.estimate, args.mandate, L(args.depends_on))
    if a == "claim":
        holder = need(actor, "scientist", "researcher", what="claiming a task").name
        return tasks.claim(store, args.id, holder)
    if a == "release":
        return tasks.release(store, actor, args.id, args.to == "scientist")
    if a == "cancel":
        return tasks.cancel(store, actor, args.id, args.reason)
    if a == "test":
        return tasks.test(store, actor, args.id, args.record, args.commit, args.log)
    if a == "dispatch":
        return tasks.dispatch(store, actor, args.id, args.note)
    if a == "block":
        return tasks.block(store, actor, args.id, args.reason, args.note)
    if a == "answer":
        return tasks.answer(store, actor, args.id, args.text)
    if a == "set":
        if args.envelope or args.estimate:
            with store.write() as tx:
                t = tx.require(args.id, "Task")
                need(actor, "coordinator" if args.envelope else "scientist", what="changing a task card")
                tx.update(args.id, {k: v for k, v in (("envelope", args.envelope), ("estimate", args.estimate)) if v})
                tx.event("task.envelope" if args.envelope else "task.estimate", args.id,
                         args.envelope or args.estimate)
            if not (args.requirement or args.done_check):
                return {"summary": f"{t['id']} changed"}
        return tasks.set_prep(store, actor, args.id, args.requirement, args.done_check)
    if a == "deliver":
        return tasks.deliver(store, actor, args.id, args.conclusion, args.limitations, L(args.outputs),
                             args.reusable, args.note)
    if a == "accept":
        return tasks.accept(store, actor, args.id)
    if a == "return":
        return tasks.return_task(store, actor, args.id, args.reason)
    if a == "check":
        with store.read() as tx:
            t = tx.require(args.id, "Task")
            lines = verify.hypothesis_lines(tx, t) if t["kind"] == "hypothesis" else \
                verify.task_lines(tx, store.project, t)
        return {"summary": "\n".join(lines)}
    with store.read() as tx:
        rows = [t for t in tx.all("Task") if args.all or t["state"] not in TASK_DONE]
    return {"tasks": rows, "summary": "\n".join(tasks.task_line(t) for t in rows) or "no tasks"}


def cmd_phase(args):
    store, actor = _store(args), _actor(args)
    if args.action == "create":
        return tasks.phase_create(store, actor, args.title, args.scientists, args.budget, L(args.tasks))
    return tasks.phase_add(store, actor, args.id, L(args.tasks))


def cmd_request(args):
    store, actor = _store(args), _actor(args)
    if args.action == "forward":
        return tasks.forward(store, actor, args.id)
    if args.action == "answer":
        return tasks.answer_ask(store, actor, args.id, args.by, args.text)
    if not (args.stuck and args.options and args.recommend):
        raise RLError("INVALID", "rl request --stuck .. --options .. --recommend .. [--cost ..] [--task T-x] "
                                 "[--kind approval]")
    return tasks.ask(store, actor, args.stuck, args.options, args.recommend, args.cost, args.task, args.kind)


def cmd_review(args):
    store, actor, a = _store(args), _actor(args), args.action
    if a == "request":
        return reviews.request(store, actor, args.target, args.kind, args.question, args.recheck_of)
    if a == "answer":
        findings = load_json_arg(args.findings) or []
        close = load_json_arg(args.close) or []
        if not isinstance(findings, list) or not isinstance(close, list):
            raise RLError("INVALID", "--findings and --close are JSON lists")
        return reviews.answer(store, actor, args.id, args.verdict, findings, close, args.doc)
    rv, _, n = args.id.partition("#F")
    if not n:
        raise RLError("INVALID", "settle names a finding: RV-x#Fn")
    return reviews.settle(store, actor, rv, int(n), args.status, args.conditions)


def cmd_advise(args):
    store, actor = _store(args), _actor(args)
    if args.action == "ask":
        return reviews.consult(store, actor, args.question)
    rulings = load_json_arg(args.rulings) or []
    return reviews.advise(store, actor, args.id, args.doc, rulings)


def cmd_question(args):
    store, actor = _store(args), _actor(args)
    if args.close:
        return messages.close(store, actor, args.close)
    if not (args.to and args.on and args.text):
        raise RLError("INVALID", "rl question --to <session> --on <record> --text .. (or --close MSG-x)")
    return messages.ask(store, actor, args.to, args.on, args.text, L(args.ref))


def cmd_reply(args):
    store, actor = _store(args), _actor(args)
    if actor is None:
        raise RLError("INVALID", "a reply is written by a session (--as <session> outside a pane)")
    return messages.reply(store, actor, args.id, args.text, L(args.ref))


def cmd_discuss(args):
    store, actor = _store(args), _actor(args)
    if actor is None:
        raise RLError("INVALID", "discussion is written by a session (--as <session> outside a pane)")
    return messages.discuss(store, actor, args.id, args.text, L(args.ref))


def cmd_refs(args):
    store = _store(args)
    with store.read() as tx:
        clean = refs.check(tx, store.project, L(args.refs), note_cap=None)
        return {"refs": clean, "summary": "\n".join(refs.expand(tx, store.project, clean))}


def cmd_run(args):
    store, actor, a = _store(args), _actor(args), args.action
    if a == "register":
        config = load_json_arg(args.config)
        params = dict(p.split("=", 1) for p in args.param or [])
        return runs.register(store, actor, args.task, args.request_id, args.arm, args.seed, config, args.split,
                             args.commit, args.minutes, args.trial, args.deviation, args.purpose, args.ticket,
                             args.run, args.resource, params or None)
    if a == "ended":
        raw = None if sys.stdin.isatty() or args.attempt else sys.stdin.read()
        return runs.ended(store, raw, args.attempt, args.outcome, args.note)
    if a == "record":
        items = load_json_records(args.observation) if args.observation else [{}]
        flags = {"attempt_id": args.attempt, "source": args.source, "observed_at": args.observed_at,
                 "execution_state": args.state, "outcome": args.outcome, "raw_state": args.raw_state,
                 "source_ref": args.source_ref, "note": args.note, "record_id": args.record_id}
        flags = {k: v for k, v in flags.items() if v is not None}
        if args.exec_id:
            flags["execution_ref"] = {"kind": args.exec_kind, "executor_namespace": args.exec_namespace,
                                      "opaque_id": args.exec_id, "locator": args.exec_locator}
        out = [runs.record(store, {"observed_at": now_utc(), **obs, **flags}) for obs in items]
        return out[0] if len(out) == 1 else {"records": out, "summary": "\n".join(r["summary"] for r in out)}
    if a == "import-result":
        reported = load_json_arg(args.manifest)
        if not isinstance(reported, dict):
            raise RLError("INVALID", "--manifest is a JSON object (inline, @file or a path)")
        return runs.import_result(store, actor, args.attempt, reported, args.request_id)
    if a == "reject-result":
        return runs.reject_result(store, actor, args.id, args.reason)
    if a == "handled":
        return runs.handled(store, actor, L(args.attempts), args.note)
    if a == "usage":
        use = {"accounting_ref": args.accounting_ref, "quantity": args.quantity, "unit": args.unit,
               "resource_type": args.resource_type, "hardware": args.hardware, "basis": args.basis,
               "source_ref": args.source_ref, "note": args.note}
        return runs.add_usage(store, use, L(args.attempts), args.revise)
    return runs.show(store, args.id, args.active)


def cmd_paper(args):
    store, a = _store(args), args.action
    if a == "add":
        return literature.add_paper(store, args.identifier, args.title, L(args.authors), args.year, args.url,
                                    args.fetch)
    if a == "search":
        return literature.search(store, args.query, args.limit)
    if a == "link":
        return literature.link_paper(store, args.source, args.paper, args.relation, args.assessment, args.locator,
                                     args.note, args.note_file)
    if a == "access":
        return literature.set_access(store, args.id, args.fulltext, args.digest)
    with store.read() as tx:
        papers = tx.all("Paper")
    return {"papers": papers, "summary": "\n".join(research.brief(p) for p in papers) or "no papers"}


def cmd_agent(args):
    """The coordinator answers a session's dialog (11.3); anyone may look at it."""
    store = _store(args)
    with store.read() as tx:
        s = next((x for x in tx.all("Session", name=args.name) if x["status"] == "open"), None)
    if s is None or not s.get("herdr_pane"):
        raise RLError("NOT_FOUND", f"no open session {args.name}")
    if args.action == "dialog":
        return {"summary": launch.excerpt(herdr.read(s["herdr_pane"]), 1500)}
    actor = _actor(args)
    if args.name == "coordinator":
        pass  # the user answers the coordinator's own dialog in its pane; recorded here when asked to
    else:
        need(actor, "coordinator", what="answering a session's dialog")
    screen = herdr.read(s["herdr_pane"])
    why = launch.select(s["herdr_pane"], args.option, screen, lambda now: True)
    if why:
        raise RLError("INVALID", why)
    herdr.keys(s["herdr_pane"], "enter")
    with store.write() as tx:
        tx.update(s["id"], {"needs_decision": None})
        tx.event("session.answered", args.name, f"{args.option}: {args.reason}")
    return {"summary": f"{args.name}: answered {args.option!r}"}


# -- parser ---------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rl", description="Smooth Research: hypotheses, experiments, tasks, runs, "
                                                        "evidence, decisions, reviews and advice, as records.")
    p.add_argument("--project", help="project directory (default: the nearest smooth-research.yaml above cwd)")
    p.add_argument("--json", action="store_true", help="print the full JSON result")
    p.add_argument("--as", dest="as_", help="the session this command runs as (default: SMOOTH_RESEARCH_SESSION)")
    p.add_argument("--read-only", action="store_true", help="read without writing anything (no upgrade, no caches)")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    def subs(sp):
        return sp.add_subparsers(dest="action", required=True)

    sp = add("init", cmd_init, "create a research project here from a plan")
    sp.add_argument("--plan", help="the research plan (copied to docs/plan.md and sealed)")
    sp.add_argument("--title")
    sp.add_argument("--dir")
    sp.add_argument("--python", default=research.DEFAULT_PYTHON, help="Python of the project's environment (bin/rl)")
    sp = add("pack", cmd_pack, "the pack of the calling session: role card, its card, inbox, material")
    sp.add_argument("--for", dest="for_", help="another session's pack (for people and tests)")
    sp = add("inbox", cmd_inbox, "what waits on the calling session now (computed from the records)")
    sp.add_argument("--for", dest="for_", help="another recipient: a session name, or user")
    sp.add_argument("--item", type=int, help="open item N: its subject expanded, check lines, next commands")
    add("status", cmd_status, "the project now, what waits on you, whether the watcher runs (starts nothing)")
    add("resume", cmd_resume, "run the project on: the watcher and the coordinator's pane in herdr, then the status")
    add("view", cmd_view, "draw docs/research.html now")
    add("doctor", cmd_doctor, "check sealed documents, run records and that every unfinished record waits on someone")
    sp = add("show", cmd_show, "one record with its relations (DOC ids print the sealed text)")
    sp.add_argument("id")
    sp = add("history", cmd_history, "the event log")
    sp.add_argument("id", nargs="?")
    sp.add_argument("--limit", type=int, default=30)
    add("tree", cmd_tree, "the hypothesis tree, one line each")
    sp = add("refs", cmd_refs, "check locators and print their expansion")
    sp.add_argument("refs", nargs="+")
    sp = add("link", cmd_link, "relate two records: " + ", ".join(LINKABLE))
    sp.add_argument("source")
    sp.add_argument("relation", choices=LINKABLE)
    sp.add_argument("target")
    sp.add_argument("--note")
    sp.add_argument("--locator")
    sp.add_argument("--assessment", choices=PAPER_ASSESSMENTS)
    sp = add("export", cmd_export, "a consistent snapshot of records and sealed documents")
    sp.add_argument("--out")
    sp.add_argument("--records-only", action="store_true")
    sp = add("restore", cmd_restore, "rebuild a project from a snapshot into a new directory")
    sp.add_argument("snapshot")
    sp.add_argument("--into", required=True)
    sp = add("digest", cmd_digest, "blake2b digests of files or directories")
    sp.add_argument("paths", nargs="+")
    sp = add("upgrade", cmd_upgrade, "after a plugin update: the record format, what to adjust")
    sp.add_argument("--note")
    sp.add_argument("--move", nargs=2, metavar=("OLD", "NEW"))
    sp = add("watch", cmd_watch, "the watcher: run (the loop), once (one beat), start (in its own herdr tab)")
    sp.add_argument("action", nargs="?", choices=["run", "once", "start"], default="run")
    sp = add("hook", cmd_hook, "host hook handler (reads the hook event JSON on stdin)")
    sp.add_argument("event", choices=["session-start"])
    sp.add_argument("--host", required=True, choices=["claude", "codex"])
    sp = add("usage", cmd_usage, "Claude Code's status line command (keeps the usage windows for the watcher)")
    sp.add_argument("action", nargs="?", choices=["statusline"])
    sp = add("compute", cmd_compute, "compute resources: show, steps (how to stage, run and fetch), sync")
    sp.add_argument("action", nargs="?", choices=["show", "steps", "sync"], default="show")
    sp.add_argument("resource", nargs="?")
    sp.add_argument("--commit")
    sp.add_argument("--attempt")
    sp = add("docs", cmd_docs, "the coordinator commits docs/glossary.md or docs/explanations/*.md")
    sp.add_argument("paths")
    sp.add_argument("--message", required=True)

    sp = add("hypothesis", cmd_hypothesis, "hypotheses: add, revise, flag, keep, drop, defer, list")
    ss = subs(sp)
    s = ss.add_parser("add")
    s.add_argument("--title", required=True)
    s.add_argument("--statement", required=True, help="the testable claim")
    s.add_argument("--purpose", required=True, help="one sentence: what testing it answers")
    s.add_argument("--scope")
    s.add_argument("--source", help="where in the plan it comes from (a heading)")
    s.add_argument("--parent", help="its primary parent (the tree edge)")
    s.add_argument("--assumes", help="hypotheses it takes as premises")
    s.add_argument("--cost", help="rough cost, e.g. 'about 2 GPU·h'")
    s.add_argument("--composite", action="store_true", help="decided by its children's verdicts")
    s.add_argument("--motivated-by")
    s = ss.add_parser("revise")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    for f in ("--statement", "--scope", "--title", "--purpose", "--source", "--cost"):
        s.add_argument(f)
    s = ss.add_parser("flag")
    s.add_argument("id")
    s.add_argument("--lack", required=True, choices=["novelty", "feasibility"])
    s.add_argument("--basis", required=True)
    s.add_argument("--options", required=True)
    for name in ("keep", "drop"):
        ss.add_parser(name).add_argument("id")
    s = ss.add_parser("defer")
    s.add_argument("id")
    s.add_argument("--reason")
    s.add_argument("--clear", action="store_true")
    ss.add_parser("list")

    sp = add("experiment", cmd_experiment, "experiments: add, set")
    ss = subs(sp)
    s = ss.add_parser("add")
    s.add_argument("--title", required=True)
    s.add_argument("--question", required=True)
    s.add_argument("--hypotheses", required=True)
    s.add_argument("--motivated-by", required=True, help="the final route decision behind it")
    s = ss.add_parser("set")
    s.add_argument("id")
    s.add_argument("--reason", help="required once sealed: a new revision")
    s.add_argument("--title")
    s.add_argument("--question")
    for sx in (ss.choices["add"], s):
        sx.add_argument("--manifest", help="path of the manifest in the repository (arms, seeds, split, config)")
        sx.add_argument("--protected", help="path::symbol units the researcher may not change")
        sx.add_argument("--semantic-tests", help="the command that runs the semantic tests")
        sx.add_argument("--brief", help="a document file explaining the design")
        sx.add_argument("--estimate")

    sp = add("evidence", cmd_evidence, "evidence: add (a fact from evidence runs, no direction), set")
    ss = subs(sp)
    s = ss.add_parser("add")
    s.add_argument("--task", required=True)
    s.add_argument("--summary", required=True)
    s.add_argument("--level", required=True, choices=ENUMS["evidence_level"])
    s.add_argument("--results")
    s.add_argument("--attempts")
    s.add_argument("--scope")
    s.add_argument("--artifacts")
    s.add_argument("--doc")
    s = ss.add_parser("set")
    s.add_argument("id")
    s.add_argument("--validity", required=True, choices=ENUMS["validity"])
    s.add_argument("--reason", required=True)

    sp = add("decide", cmd_decide, "a decision: verdict or stop (drafts until a conclusion review passes), route, "
                                   "phase; --final D-x finalizes a draft")
    sp.add_argument("--final", metavar="D-x")
    sp.add_argument("--kind", choices=["verdict", "stop", "route", "phase"])
    sp.add_argument("--targets")
    sp.add_argument("--rationale")
    sp.add_argument("--rationale-file")
    sp.add_argument("--based-on")
    sp.add_argument("--status", help="verdict: supported|challenged|inconclusive; stop: paused|retired")
    sp.add_argument("--supports")
    sp.add_argument("--challenges")
    sp.add_argument("--did", help="verdict: what was done (one or two sentences, no copied numbers)")
    sp.add_argument("--means", help="verdict: what it means")
    sp.add_argument("--highlights", help="phase: JSON list of {decision, why}")

    sp = add("task", cmd_task, "tasks: create, claim, release, cancel, test, dispatch, block, answer, set, deliver, "
                               "accept, return, check, list")
    ss = subs(sp)
    s = ss.add_parser("create")
    s.add_argument("--kind", required=True, choices=ENUMS["task_kind"])
    s.add_argument("--title", help="default for a hypothesis task: its hypothesis's title")
    s.add_argument("--targets")
    s.add_argument("--priority", type=int)
    s.add_argument("--envelope")
    s.add_argument("--parallel", type=int)
    s.add_argument("--reason")
    s.add_argument("--phase")
    s.add_argument("--experiment")
    s.add_argument("--requirement")
    s.add_argument("--done-check")
    s.add_argument("--estimate")
    s.add_argument("--mandate", help="a mandate document file")
    s.add_argument("--depends-on")
    for name in ("claim", "accept", "check"):
        ss.add_parser(name).add_argument("id")
    s = ss.add_parser("release")
    s.add_argument("id")
    s.add_argument("--to", choices=["scientist"])
    for name in ("cancel", "return"):
        s = ss.add_parser(name)
        s.add_argument("id")
        s.add_argument("--reason", required=True)
    s = ss.add_parser("test")
    s.add_argument("id")
    s.add_argument("--record", choices=["pass", "fail"])
    s.add_argument("--commit")
    s.add_argument("--log")
    s = ss.add_parser("dispatch")
    s.add_argument("id")
    s.add_argument("--note")
    s = ss.add_parser("block")
    s.add_argument("id")
    s.add_argument("--reason", required=True, choices=["design", "mandate", "question"])
    s.add_argument("--note", required=True)
    s = ss.add_parser("answer")
    s.add_argument("id")
    s.add_argument("--text", required=True)
    s = ss.add_parser("set")
    s.add_argument("id")
    s.add_argument("--requirement")
    s.add_argument("--done-check")
    s.add_argument("--envelope")
    s.add_argument("--estimate")
    s = ss.add_parser("deliver")
    s.add_argument("id")
    s.add_argument("--conclusion")
    s.add_argument("--limitations")
    s.add_argument("--outputs")
    s.add_argument("--reusable", help="one sentence: what of its outputs others may reuse")
    s.add_argument("--note")
    s = ss.add_parser("list")
    s.add_argument("--all", action="store_true")

    sp = add("phase", cmd_phase, "phases: create, add")
    ss = subs(sp)
    s = ss.add_parser("create")
    s.add_argument("--title", required=True)
    s.add_argument("--scientists", type=int)
    s.add_argument("--budget", help="shown only")
    s.add_argument("--tasks")
    s = ss.add_parser("add")
    s.add_argument("id")
    s.add_argument("--tasks", required=True)

    sp = add("request", cmd_request, "decision requests: rl request --stuck .. --options .. --recommend ..; forward; "
                                     "answer")
    sp.add_argument("action", nargs="?", choices=["new", "forward", "answer"], default="new")
    sp.add_argument("id", nargs="?")
    sp.add_argument("--stuck")
    sp.add_argument("--options")
    sp.add_argument("--recommend")
    sp.add_argument("--cost")
    sp.add_argument("--task")
    sp.add_argument("--kind", choices=["decision", "approval"], default="decision")
    sp.add_argument("--by", choices=["coordinator", "user"])
    sp.add_argument("--text")

    sp = add("review", cmd_review, "review rounds: request, answer, settle")
    ss = subs(sp)
    s = ss.add_parser("request")
    s.add_argument("target", help="a task (design or tree review) or a draft decision (conclusion review)")
    s.add_argument("--kind", choices=["design", "conclusion", "tree"], default="design")
    s.add_argument("--question")
    s.add_argument("--recheck-of")
    s = ss.add_parser("answer")
    s.add_argument("id")
    s.add_argument("--verdict", required=True, choices=["pass", "fail"])
    s.add_argument("--findings", help="JSON list of {category, severity, text, loc[, engineering_reason]}")
    s.add_argument("--close", help="JSON list of {n, status: fixed|constrained|withdrawn, conditions}")
    s.add_argument("--doc", help="the answer document file")
    s = ss.add_parser("settle")
    s.add_argument("id", help="RV-x#Fn")
    s.add_argument("--status", required=True, choices=["constrained", "withdrawn"])
    s.add_argument("--conditions")

    sp = add("advise", cmd_advise, "advice: ask (a consultation), answer")
    ss = subs(sp)
    s = ss.add_parser("ask")
    s.add_argument("--question", required=True)
    s = ss.add_parser("answer")
    s.add_argument("id")
    s.add_argument("--doc", required=True)
    s.add_argument("--rulings", help="JSON list of {n, ruling: uphold|withdraw, reason}")

    sp = add("question", cmd_question, "the coordinator asks a session a question; --close after reading the answer")
    sp.add_argument("--to")
    sp.add_argument("--on", help="the record it is about")
    sp.add_argument("--text")
    sp.add_argument("--ref", action="append")
    sp.add_argument("--close", metavar="MSG-x")
    sp = add("reply", cmd_reply, "reply to a dialogue entry addressed to you")
    sp.add_argument("id")
    sp.add_argument("--text", required=True)
    sp.add_argument("--ref", action="append")
    sp = add("discuss", cmd_discuss, "review discussion in an open round (RV-x) or on a finding (RV-x#Fn)")
    sp.add_argument("id")
    sp.add_argument("--text", required=True)
    sp.add_argument("--ref", action="append")

    sp = add("run", cmd_run, "runs: register, ended, record, import-result, reject-result, handled, usage, show")
    ss = subs(sp)
    s = ss.add_parser("register", help="register a run and a prepared attempt (starts nothing)")
    s.add_argument("--task", required=True)
    s.add_argument("--request-id", required=True)
    s.add_argument("--arm")
    s.add_argument("--seed")
    s.add_argument("--config", help="the run's configuration as JSON")
    s.add_argument("--split")
    s.add_argument("--commit")
    s.add_argument("--minutes", type=float, help="expected duration (the attempt's expected end)")
    s.add_argument("--trial", action="store_true", help="a trial run (smoke, timing): at most limits.trial_minutes")
    s.add_argument("--deviation", help="register a run that differs from the manifest (not evidence)")
    s.add_argument("--purpose", choices=list(runs.PURPOSES))
    s.add_argument("--ticket")
    s.add_argument("--run", help="a new attempt of this run")
    s.add_argument("--resource")
    s.add_argument("--param", action="append")
    s = ss.add_parser("ended", help="the executor's command delivery: the notification JSON on stdin")
    s.add_argument("--attempt")
    s.add_argument("--outcome", choices=ENUMS["outcome"])
    s.add_argument("--note")
    s = ss.add_parser("record", help="record an observation of an attempt (queries nothing)")
    s.add_argument("--observation")
    s.add_argument("--attempt")
    s.add_argument("--source", default="local-shell")
    s.add_argument("--observed-at")
    s.add_argument("--state", choices=ENUMS["execution_state"])
    s.add_argument("--outcome", choices=ENUMS["outcome"])
    s.add_argument("--raw-state")
    s.add_argument("--source-ref")
    s.add_argument("--record-id")
    s.add_argument("--exec-kind", choices=ENUMS["exec_kind"], default="local_process")
    s.add_argument("--exec-namespace")
    s.add_argument("--exec-id")
    s.add_argument("--exec-locator")
    s.add_argument("--note")
    s = ss.add_parser("import-result")
    s.add_argument("--attempt", required=True)
    s.add_argument("--manifest", required=True, help="the result manifest JSON: status, arm, seed, commit, config, "
                                                     "metrics, artifacts")
    s.add_argument("--request-id", required=True)
    s = ss.add_parser("reject-result")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s = ss.add_parser("handled")
    s.add_argument("attempts")
    s.add_argument("--note", required=True)
    s = ss.add_parser("usage")
    s.add_argument("--accounting-ref", required=True)
    s.add_argument("--quantity", type=float, required=True)
    s.add_argument("--unit", required=True)
    s.add_argument("--resource-type", required=True)
    s.add_argument("--hardware")
    s.add_argument("--basis", choices=ENUMS["usage_basis"], default="reported")
    s.add_argument("--source-ref")
    s.add_argument("--attempts", action="append")
    s.add_argument("--note")
    s.add_argument("--revise", action="store_true")
    s = ss.add_parser("show")
    s.add_argument("id", nargs="?")
    s.add_argument("--active", action="store_true")

    sp = add("paper", cmd_paper, "literature: add, search, link, access, list")
    ss = subs(sp)
    s = ss.add_parser("add")
    s.add_argument("identifier", nargs="?")
    s.add_argument("--title")
    s.add_argument("--authors", action="append")
    s.add_argument("--year", type=int)
    s.add_argument("--url")
    s.add_argument("--fetch", action="store_true")
    s = ss.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s = ss.add_parser("link")
    s.add_argument("source")
    s.add_argument("paper")
    s.add_argument("--relation", required=True, choices=["nearest_work", "motivated_by", "cites"])
    s.add_argument("--assessment", required=True, choices=PAPER_ASSESSMENTS)
    s.add_argument("--locator")
    s.add_argument("--note")
    s.add_argument("--note-file")
    s = ss.add_parser("access")
    s.add_argument("id")
    s.add_argument("--fulltext", required=True, choices=ENUMS["fulltext_access"])
    s.add_argument("--digest")
    ss.add_parser("list")

    sp = add("agent", cmd_agent, "a session's dialog: dialog (show it), answer (the coordinator selects an option)")
    ss = subs(sp)
    s = ss.add_parser("dialog")
    s.add_argument("name")
    s = ss.add_parser("answer")
    s.add_argument("name")
    s.add_argument("option")
    s.add_argument("--reason", required=True)
    _accept_global_options_everywhere(p)
    return p


def _accept_global_options_everywhere(parser: argparse.ArgumentParser) -> None:
    """Let --project, --json and --as also follow a subcommand (`rl decide ... --json`)."""
    def walk(pp: argparse.ArgumentParser) -> None:
        for action in pp._actions:
            if isinstance(action, argparse._SubParsersAction):
                for sub in set(action.choices.values()):
                    sub.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                    sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                    sub.add_argument("--as", dest="as_", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                    walk(sub)
    walk(parser)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.read_only:
            _check_read_only(args)
        result = args.fn(args)
    except RLError as exc:
        if args.command == "hook":
            print(json.dumps({"systemMessage": f"smooth-research: {exc.message}"}))
            return 0
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, default=str, indent=1), file=sys.stderr)
        return 3 if exc.code in CONFLICT_CODES else 1
    warnings = list(getattr(args, "config_warnings", None) or [])
    store = getattr(args, "store", None)
    if store is not None and args.command not in ("watch", "hook", "usage"):
        note = watch.stale_note(store)
        if note:
            warnings.append(note)
    if args.command == "hook" or args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=None if args.command == "hook" else 1))
    else:
        print(result.get("summary", ""))
        for w in [*(result.get("warnings") or []), *warnings]:
            print(f"warning: {w}")
        for step in result.get("next") or []:
            print(f"next: {step}")
    return 0
