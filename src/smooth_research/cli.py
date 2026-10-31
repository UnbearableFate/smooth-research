"""`rl`: the single command-line entry point shared by every host.

Output is a short summary with ids and pointers; --json prints the full result. Errors print
`error[CODE]: message` on stderr and exit 1 (3 for conflicts and stale revisions).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import (adapters, agents, compute, context, delegate, hooks, literature, manuscript, messages, research,
               reviews, runs, snapshot, tasks, upgrade, usage, view, watch)
from .project import advice_limit, find_project, model_entries
from .schema import ENUMS, LINKABLE, PAPER_ASSESSMENTS
from .store import Store
from .util import RLError, load_json_arg, load_json_records, now_utc, split_list

CONFLICT_CODES = {"CONFLICT", "STALE_REVISION"}


def _store(args) -> Store:
    project = find_project(args.project)
    args.config_warnings = project.warnings
    args.store = Store(project, args.actor or "agent")
    return args.store


def _upgrade_notice(args) -> list[str]:
    """Told once, by the command whose first open upgraded the records of an older plugin."""
    done = getattr(getattr(args, "store", None), "upgraded", None)
    if not done or args.command == "upgrade":
        return []
    return [f"the records were upgraded from format {done['from']} to {done['to']} for this plugin version (a copy "
            f"of the old database: {done['backup']}); run `rl upgrade` to see what else to adjust"]


def _list(value) -> list[str]:
    return split_list(value)


# -- handlers -------------------------------------------------------------------------------------------------


def cmd_init(args):
    return research.init_project(Path(args.dir or "."), args.plan, args.title)


def cmd_status(args):
    return context.status(_store(args))


def cmd_resume(args):
    return context.resume_pack(_store(args), args.role, args.budget)


def cmd_show(args):
    return research.show(_store(args), args.id)


def cmd_history(args):
    return research.history(_store(args), args.id, args.limit)


def cmd_graph(args):
    store = _store(args)
    if args.mermaid:
        with store.read() as tx:
            return {"summary": view.mermaid(tx)}
    return view.tree(store, args.root)


def cmd_view(args):
    return view.html_view(_store(args))


def cmd_report(args):
    return view.report(_store(args), args.out, args.title)


def cmd_manuscript(args):
    return manuscript.write(_store(args), args.title)


def cmd_export(args):
    return snapshot.export(_store(args), args.out)


def cmd_restore(args):
    return snapshot.restore(args.snapshot, args.into)


def cmd_doctor(args):
    return snapshot.doctor(_store(args))


def cmd_digest(args):
    return runs.digest_paths(args.paths)


def cmd_link(args):
    return research.link(_store(args), args.source, args.relation, args.target, args.note, args.locator,
                         args.assessment)


def cmd_session(args):
    store = _store(args)
    if args.action == "start":
        out = tasks.start_session(store, args.role, args.host, args.host_session_id, args.model,
                                  args.model_observed, args.effort, args.worktree)
        if args.role == "coordinator" and args.host == "claude":  # its status line reports Claude's usage
            note = usage.install_statusline(store.project.root, delegate.rl_command())
            out["summary"] += f"\nClaude usage: {note}" if note else ""
        return out
    return tasks.end_session(store, args.id)


def cmd_hypothesis(args):
    store = _store(args)
    if args.action == "add":
        return research.add_hypothesis(store, args.title, args.statement, args.scope, args.disposition,
                                       _list(args.derived_from), _list(args.combines), _list(args.motivated_by),
                                       args.origin, args.statement_file)
    if args.action == "revise":
        return research.revise_hypothesis(store, args.id, args.reason, args.statement, args.scope, args.title,
                                          args.base_revision, args.statement_file)
    return research.set_hypothesis(store, args.id, args.reason, args.evidence_status, args.disposition,
                                   args.rechecked)


def cmd_experiment(args):
    store = _store(args)
    if args.action == "add":
        return research.add_experiment(store, args.title, args.question, _list(args.hypotheses), args.stage,
                                       _list(args.compared_with), args.code_from, _list(args.depends_on),
                                       intervention=args.intervention, comparator=args.comparator,
                                       metric=args.metric, scope=args.scope, outcome_actions=args.outcome_actions,
                                       budget=args.budget, stop=args.stop)
    return research.seal_experiment(store, args.id)


def cmd_evidence(args):
    store = _store(args)
    if args.action == "add":
        return research.add_evidence(store, args.summary, args.level, args.validity, args.scope,
                                     _list(args.supports), _list(args.challenges), _list(args.attempts),
                                     _list(args.results), args.experiment, _list(args.artifacts), args.doc)
    return research.set_evidence(store, args.id, args.validity, args.reason)


def cmd_decide(args):
    return research.decide(_store(args), _list(args.targets), args.action, args.rationale, _list(args.based_on),
                           args.reopen_condition, "stage" if args.stage else "route", args.implementation_ready,
                           args.evidence_scope, args.set_evidence_status, args.rationale_file)


def cmd_task(args):
    store = _store(args)
    a = args.action
    if a == "create":
        return tasks.create_task(store, args.title, args.kind, _list(args.targets), args.request, args.to_role,
                                 args.next_action, _list(args.depends_on))
    if a == "claim":
        return tasks.claim(store, args.id, args.role, args.session)
    if a in ("deliver", "wait", "block", "resume"):
        state = {"deliver": "delivered", "wait": "waiting", "block": "blocked", "resume": "active"}[a]
        return tasks.progress(store, args.id, state, args.role, args.summary,
                              _list(getattr(args, "output", None)), args.next_action,
                              _list(getattr(args, "attempts", None)), getattr(args, "reason", None))
    if a == "handoff":
        return tasks.handoff(store, args.id, args.doc, args.role, args.to_role)
    if a == "list":
        with store.read() as tx:
            items = tx.all("Task") if args.all else tasks.open_tasks(tx)
        return {"tasks": items, "summary": "\n".join(research.brief(t) + (f" next: {t['next_action']}"
                                                                          if t.get("next_action") else "")
                                                     for t in items) or "no open tasks"}
    return tasks.review_task(store, args.id, a, args.reason)


def cmd_inbox(args):
    return messages.inbox(_store(args), args.role, args.ack, args.all)


def cmd_msg(args):
    return messages.send(_store(args), args.type, args.sender, args.to, args.text, args.task, args.next_action,
                      _list(args.doc), args.message_id)


def cmd_context(args):
    store = _store(args)
    if args.purpose == "work":
        if not args.task:
            raise RLError("INVALID", "a work pack needs --task")
        return context.work_pack(store, args.task, args.budget)
    if args.purpose == "resume":
        return context.resume_pack(store, args.role, args.budget)
    if args.purpose == "review":
        if not args.ticket:
            raise RLError("INVALID", "a review pack needs --ticket RV-xxxx")
        return research.show(store, _review_request(store, args.ticket))
    if not args.question:
        raise RLError("INVALID", "an advice pack needs --question")
    return context.advice_pack(store, args.target, args.question, args.budget)


def _review_request(store: Store, ticket: str) -> str:
    with store.read() as tx:
        t = tx.require(ticket, "Ticket")
    if not t.get("request_ref"):
        raise RLError("NOT_FOUND", f"{ticket} has no request package")
    return t["request_ref"]


def _spec_from_flags(args) -> dict:
    spec: dict = {}
    if args.arm:
        spec["arm"] = args.arm
    if args.seed is not None:
        spec["seed"] = args.seed
    code = {k: v for k, v in (("commit", args.code_commit), ("content_digest", args.code_digest)) if v}
    if code:
        spec["code_ref"] = code
    elif args.code_unknown:
        spec["code_ref"] = "unknown"
    for key in ("config_digest", "config_ref", "data_revision", "model_revision", "metric", "metric_spec_ref",
                "environment_ref", "output_spec", "resume_from"):
        if getattr(args, key, None):
            spec[key] = getattr(args, key)
    if args.param:
        params = {}
        for item in args.param:
            key, _, value = item.partition("=")
            try:
                params[key] = json.loads(value)
            except json.JSONDecodeError:
                params[key] = value
        spec["params"] = params
    return spec


def cmd_run(args):
    store = _store(args)
    a = args.action
    if a == "register":
        spec = load_json_arg(args.spec) or {}
        spec.update(_spec_from_flags(args))
        return runs.register(store, args.experiment, spec, args.request_id, args.run, _list(args.review),
                             args.no_review, args.resource)
    if a == "record":
        items = load_json_records(args.observation) if args.observation else [{}]
        flags = {"record_id": args.record_id, "attempt_id": args.attempt, "source": args.source,
                 "observed_at": args.observed_at, "source_sequence": args.sequence, "execution_state": args.state,
                 "outcome": args.outcome, "raw_state": args.raw_state, "source_ref": args.source_ref,
                 "correction_of": args.correction_of, "note": args.note}
        flags = {k: v for k, v in flags.items() if v is not None}
        if len(items) > 1 and "record_id" in flags:
            raise RLError("INVALID", "--record-id names one observation; a batch carries its ids in the records")
        if args.exec_id:
            flags["execution_ref"] = {"kind": args.exec_kind, "executor_namespace": args.exec_namespace,
                                      "opaque_id": args.exec_id, "locator": args.exec_locator}
        out = []
        for n, obs in enumerate(items, 1):
            obs = {"observed_at": now_utc(), **obs, **flags}
            try:
                out.append(runs.record(store, obs))
            except RLError as exc:
                if len(items) == 1:
                    raise
                raise RLError(exc.code, f"observation {n} of {len(items)}: {exc.message} ({n - 1} before it were "
                                        "recorded; re-sending the batch is safe, received records are skipped)",
                              **exc.details) from None
        if len(out) == 1:
            return out[0]
        return {"records": out, "summary": "\n".join(r["summary"] for r in out)}
    if a == "import-result":
        manifest = load_json_arg(args.manifest)
        if not isinstance(manifest, dict):
            raise RLError("INVALID", "--manifest is a JSON object (inline, @file or a path)")
        return runs.import_result(store, args.attempt, manifest, args.request_id)
    if a == "reject-result":
        return runs.reject_result(store, args.id, args.reason)
    if a == "usage":
        usage = {"accounting_ref": args.accounting_ref, "quantity": args.quantity, "unit": args.unit,
                 "resource_type": args.resource_type, "hardware": args.hardware, "basis": args.basis,
                 "source_ref": args.source_ref, "note": args.note}
        return runs.add_usage(store, usage, _list(args.attempts), args.revise)
    return runs.show(store, args.id, args.active)


def cmd_review(args):
    store = _store(args)
    a = args.action
    if a == "request":
        return reviews.request_review(store, args.target, _list(args.files), args.question,
                                      _list(args.author_model), _list(args.configs), _list(args.deps),
                                      _list(args.ignore_keys), args.scope or "", args.diff_base, args.recheck_of,
                                      args.new_round)
    if a == "conclusion":
        return reviews.request_conclusion_review(store, _list(args.target), args.proposal, _list(args.author_model),
                                                 args.question, args.recheck_of, args.new_round)
    if a == "answer":
        return reviews.answer_live(store, args.id, args.answer, args.session)
    if a == "approve":
        return reviews.approve(store, args.id, args.rationale)
    if a == "run":
        if args.background:
            return reviews.start_background(store, args.id, args.timeout, args.entry)
        return reviews.run_ticket(store, args.id, args.timeout, args.entry)
    if a == "import":
        return reviews.import_answer(store, args.id, args.answer, args.model_observed, args.source, args.provider,
                                     args.session_id)
    return reviews.show(store, args.id, "review")


def cmd_advise(args):
    store = _store(args)
    a = args.action
    if a == "prepare":
        return reviews.prepare_advice(store, args.question, args.target, args.user_approved)
    if a == "answer":
        return reviews.answer_live(store, args.id, args.answer, args.session)
    if a == "run":
        if len(args.id) > 1:
            if args.entry:
                raise RLError("INVALID", "--entry overrides the model of one ticket; name one ticket")
            return reviews.run_many(store, args.id, args.timeout, args.background)
        if args.background:
            return reviews.start_background(store, args.id[0], args.timeout, args.entry)
        return reviews.run_ticket(store, args.id[0], args.timeout, args.entry)
    if a == "import":
        return reviews.import_answer(store, args.id, args.answer, args.model_observed, args.source, args.provider,
                                     args.session_id)
    return reviews.show(store, args.id, "advice")


def cmd_paper(args):
    store = _store(args)
    a = args.action
    if a == "add":
        return literature.add_paper(store, args.identifier, args.title, _list(args.authors), args.year, args.url,
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


def cmd_delegate(args):
    store = _store(args)
    if args.background:
        return delegate.start_background(store, args.task, args.entry, args.role, args.timeout)
    return delegate.delegate(store, args.task, args.entry, args.role, args.timeout)


def cmd_agent(args):
    store = _store(args)
    a = args.action
    if a == "open":
        return agents.open_session(store, args.role, args.name, args.entry, args.task, args.ticket,
                                   not args.no_worktree, args.base, args.timeout)
    if a == "assign":
        return agents.assign(store, args.name, args.task, args.ticket)
    if a == "tell":
        return agents.tell(store, args.to, " ".join(args.text), args.sender, args.task)
    if a == "reset":
        return agents.reset(store, args.name, "compact" if args.compact else "clear")
    if a == "close":
        return agents.close(store, args.name, args.remove_worktree)
    if a == "brief":
        return agents.brief(store, args.name)
    if a == "dialog":
        return agents.dialog(store, args.name)
    if a == "answer":
        return agents.answer(store, args.name, args.option, args.reason)
    if a == "switch":
        return agents.switch(store, args.name, args.entry, args.now, args.timeout)
    if a == "check":
        return watch.check(store)
    if a == "watch":
        if args.stop:
            return watch.stop(store)
        if args.start:
            return watch.start(store, args.every)
        return watch.run(store, args.every, args.own_tab)
    return agents.list_sessions(store, args.all)


def cmd_usage(args):
    if args.action == "statusline":  # Claude Code's status line: no project needed, and it never fails
        try:
            raw = "" if sys.stdin.isatty() else sys.stdin.read()
        except (OSError, ValueError):
            raw = ""
        return {"summary": usage.statusline(raw)}
    return usage.show(_store(args))


def cmd_models(args):
    store = _store(args)
    config = store.project.config
    lines = []
    for role, rule in (("researcher", "the first available one is used"), ("reviewer", "the first available one is used"),
                       ("advisor", "every available one is asked, each on its own")):
        lines.append(f"{role} ({rule}):")
        for text in model_entries(config, role):
            try:
                entry = adapters.Entry.parse(text)
            except RLError as exc:
                lines.append(f"  {text}: invalid ({exc.message})")
                continue
            problem = adapters.available(entry)
            lines.append(f"  {entry} family={entry.family or 'unknown'} "
                         + (f"unavailable ({problem})" if problem else "callable"))
    with store.read() as tx:
        used = len(reviews.advice_rounds(tx))
    lines.append(f"advice rounds used: {used} of {advice_limit(config)} (advice.max_rounds; more need the user's "
                 "approval)")
    return {"summary": "\n".join(lines) + "\n(callable = the CLI exists; the model itself is verified on first use)"}


def cmd_compute(args):
    store = _store(args)
    if args.action == "steps":
        return compute.steps(store, args.resource, args.commit, args.attempt)
    if args.resource or args.attempt:
        raise RLError("INVALID", "rl compute lists every resource; a resource or --attempt goes with "
                                 "`rl compute steps`")
    return compute.show(store, args.commit)


def cmd_upgrade(args):
    store = _store(args)
    if args.move:
        return upgrade.move(store, *args.move)
    return upgrade.report(store, args.note)


def cmd_hook(args):
    return hooks.session_start(args.host, sys.stdin.read() if not sys.stdin.isatty() else "")


# -- parser ---------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rl", description="Smooth Research records: hypotheses, experiments, runs, "
                                                        "evidence, decisions, reviews, advice and literature.")
    p.add_argument("--project", help="project directory (default: nearest smooth-research.yaml above cwd)")
    p.add_argument("--json", action="store_true", help="print the full JSON result")
    p.add_argument("--actor", help="name recorded in the event log")
    sub = p.add_subparsers(dest="command", required=True, metavar="command")

    def add(name, fn, help_, **kw):
        sp = sub.add_parser(name, help=help_, description=help_, **kw)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("init", cmd_init, "create a research project here and import the plan snapshot")
    sp.add_argument("--plan", help="the research plan (copied once to docs/plan.md and sealed)")
    sp.add_argument("--title")
    sp.add_argument("--dir", help="project directory (default: cwd)")
    add("status", cmd_status, "short project status and attention items")
    sp = add("resume", cmd_resume, "resume pack: roles, unfinished tasks and attempts, last observations")
    sp.add_argument("--role")
    sp.add_argument("--budget", type=int)
    sp = add("show", cmd_show, "one record with its relations (DOC ids print the sealed text)")
    sp.add_argument("id")
    sp = add("history", cmd_history, "event log")
    sp.add_argument("id", nargs="?")
    sp.add_argument("--limit", type=int, default=30)
    sp = add("graph", cmd_graph, "tree projection of the hypothesis graph")
    sp.add_argument("--root")
    sp.add_argument("--mermaid", action="store_true")
    add("view", cmd_view, "write the research graph page docs/research-graph.html")
    sp = add("report", cmd_report, "write a progress or stage report into research/reports/")
    sp.add_argument("--out")
    sp.add_argument("--title")
    sp = add("manuscript", cmd_manuscript, "write or refresh the manuscript (the paper draft) from the records: "
                                            "docs/manuscript.md, or LaTeX built into a PDF (manuscript.latex)")
    sp.add_argument("--title", help="the title of a new draft")
    sp = add("export", cmd_export, "consistent snapshot of records and sealed documents")
    sp.add_argument("--out")
    sp = add("restore", cmd_restore, "rebuild a project from a snapshot into a new directory (records only)")
    sp.add_argument("snapshot")
    sp.add_argument("--into", required=True)
    add("doctor", cmd_doctor, "verify sealed documents and run records")
    sp = add("digest", cmd_digest, "blake2b digests of files or directories (for RunSpecs and manifests)")
    sp.add_argument("paths", nargs="+")
    add("models", cmd_models, "configured reviewer and advisor entries and whether their CLIs exist")
    sp = add("upgrade", cmd_upgrade, "after a plugin update: the project's record format and plugin build, the "
                                     "upgrades done, what this version reads differently and what you may adjust")
    sp.add_argument("--note", help="record what you adjusted by hand for this version (a project.adjusted event)")
    sp.add_argument("--move", nargs=2, metavar=("OLD", "NEW"), help="move a file or directory of an older layout and "
                    "rewrite the paths the records keep for it")
    sp = add("compute", cmd_compute, "compute resources (compute in smooth-research.yaml) with local checks; "
                                     "steps: how to stage a commit there, run it and fetch the results (rl "
                                     "connects to, copies to and starts nothing)")
    sp.add_argument("action", nargs="?", choices=["show", "steps"], default="show")
    sp.add_argument("resource", nargs="?", help="a resource name (steps; default: the attempt's, else "
                                                 "compute.default)")
    sp.add_argument("--commit", help="full commit id (default: the attempt's, else HEAD of your checkout)")
    sp.add_argument("--attempt", help="an attempt: its resource, commit and output directory")
    sp = add("usage", cmd_usage, "the Claude and Codex subscription windows, and the hosts that are low (their "
                                 "researchers and reviewers hand over to another model)")
    sp.add_argument("action", nargs="?", choices=["show", "statusline"], default="show",
                    help="statusline: Claude Code's status line command (reads its input on stdin)")
    sp = add("link", cmd_link, "relate two records: " + ", ".join(LINKABLE))
    sp.add_argument("source")
    sp.add_argument("relation", choices=LINKABLE)
    sp.add_argument("target")
    sp.add_argument("--note")
    sp.add_argument("--locator")
    sp.add_argument("--assessment", choices=PAPER_ASSESSMENTS)

    sp = add("session", cmd_session, "register or end a host session")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("start")
    s.add_argument("--role", required=True, help="coordinator, researcher[-name], reviewer or advisor")
    s.add_argument("--host", required=True, choices=["claude", "codex", "other"])
    s.add_argument("--host-session-id")
    s.add_argument("--model", help="requested model")
    s.add_argument("--model-observed", help="model the host reports it is running")
    s.add_argument("--effort")
    s.add_argument("--worktree")
    s = ss.add_parser("end")
    s.add_argument("id")

    sp = add("hypothesis", cmd_hypothesis, "add, revise or set the status of a hypothesis")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("add")
    s.add_argument("--title", required=True)
    s.add_argument("--statement", required=True, help="the testable claim, one or two sentences")
    s.add_argument("--scope", help="conditions the claim is about")
    s.add_argument("--disposition", default="active", choices=ENUMS["disposition"])
    s.add_argument("--derived-from", action="append", help="scientific source: H-, AD- or P- ids")
    s.add_argument("--combines", action="append")
    s.add_argument("--motivated-by", action="append")
    s.add_argument("--origin", help="where it came from, e.g. 'plan §3' or 'advice AD-0002 #1'")
    s.add_argument("--statement-file", help="longer statement document to seal")
    s = ss.add_parser("revise")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.add_argument("--statement")
    s.add_argument("--scope")
    s.add_argument("--title")
    s.add_argument("--base-revision", type=int)
    s.add_argument("--statement-file")
    s = ss.add_parser("set")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s.add_argument("--evidence-status", choices=ENUMS["evidence_status"])
    s.add_argument("--disposition", choices=ENUMS["disposition"])
    s.add_argument("--rechecked", action="store_true", help="clear a needs-recheck flag after checking")

    sp = add("experiment", cmd_experiment, "create an experiment contract draft or seal it")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("add")
    s.add_argument("--title", required=True)
    s.add_argument("--question", required=True)
    s.add_argument("--hypotheses", action="append", required=True)
    s.add_argument("--stage", choices=ENUMS["stage"])
    for f in ("intervention", "comparator", "metric", "scope", "outcome-actions", "budget", "stop"):
        s.add_argument(f"--{f}")
    s.add_argument("--compared-with", action="append")
    s.add_argument("--code-from", help="experiment whose code this starts from")
    s.add_argument("--depends-on", action="append")
    s = ss.add_parser("seal")
    s.add_argument("id")

    sp = add("evidence", cmd_evidence, "record evidence or change its validity")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("add")
    s.add_argument("--summary", required=True)
    s.add_argument("--level", required=True, choices=ENUMS["evidence_level"])
    s.add_argument("--validity", default="pending", choices=ENUMS["validity"])
    s.add_argument("--scope")
    s.add_argument("--supports", action="append")
    s.add_argument("--challenges", action="append")
    s.add_argument("--attempts", action="append")
    s.add_argument("--results", action="append")
    s.add_argument("--experiment")
    s.add_argument("--artifacts", action="append")
    s.add_argument("--doc", help="analysis document to seal")
    s = ss.add_parser("set")
    s.add_argument("id")
    s.add_argument("--validity", required=True, choices=ENUMS["validity"])
    s.add_argument("--reason", required=True)

    sp = add("decide", cmd_decide, "record a route or stage decision")
    sp.add_argument("--targets", action="append", required=True)
    sp.add_argument("--action", required=True, choices=ENUMS["decision_action"])
    sp.add_argument("--rationale", required=True)
    sp.add_argument("--based-on", action="append", help="evidence, results, papers or tickets")
    sp.add_argument("--reopen-condition")
    sp.add_argument("--stage", action="store_true", help="stage delivery: implementation, evidence scope, investment")
    sp.add_argument("--implementation-ready")
    sp.add_argument("--evidence-scope")
    sp.add_argument("--set-evidence-status", choices=ENUMS["evidence_status"])
    sp.add_argument("--rationale-file")

    sp = add("task", cmd_task, "tasks: create, claim, deliver, wait, block, resume, handoff, accept, stop, reopen, list")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("create")
    s.add_argument("--title", required=True)
    s.add_argument("--kind", required=True, choices=ENUMS["task_kind"])
    s.add_argument("--targets", action="append")
    s.add_argument("--request", help="task document to seal")
    s.add_argument("--to-role")
    s.add_argument("--next-action")
    s.add_argument("--depends-on", action="append")
    s = ss.add_parser("claim")
    s.add_argument("id")
    s.add_argument("--role", required=True)
    s.add_argument("--session")
    for name in ("deliver", "wait", "block", "resume", "handoff"):
        s = ss.add_parser(name)
        s.add_argument("id")
        s.add_argument("--role", required=True, help="your role, e.g. researcher")
        if name == "handoff":
            s.add_argument("--doc", required=True)
            s.add_argument("--to-role")
            continue
        s.add_argument("--summary")
        s.add_argument("--next-action")
        if name == "deliver":
            s.add_argument("--output", action="append")
        if name == "wait":
            s.add_argument("--attempts", action="append")
        if name == "block":
            s.add_argument("--reason", required=True, choices=ENUMS["blocked_reason"])
    for name in ("accept", "stop", "reopen"):
        s = ss.add_parser(name)
        s.add_argument("id")
        s.add_argument("--reason", required=True)
    s = ss.add_parser("list")
    s.add_argument("--all", action="store_true")

    sp = add("inbox", cmd_inbox, "messages for a role; --ack advances the cursor (no reply is sent)")
    sp.add_argument("--role", required=True)
    sp.add_argument("--ack", action="store_true")
    sp.add_argument("--all", action="store_true")
    sp = add("msg", cmd_msg, "send a short message that points to documents")
    sp.add_argument("--type", required=True, choices=ENUMS["message_type"])
    sp.add_argument("--from", dest="sender", required=True)
    sp.add_argument("--to", required=True)
    sp.add_argument("--task")
    sp.add_argument("--text")
    sp.add_argument("--next-action")
    sp.add_argument("--doc", action="append")
    sp.add_argument("--message-id")

    sp = add("context", cmd_context, "purpose-specific context pack: work, resume, review or advice")
    sp.add_argument("--purpose", required=True, choices=["work", "resume", "review", "advice"])
    sp.add_argument("--task")
    sp.add_argument("--role")
    sp.add_argument("--ticket")
    sp.add_argument("--target")
    sp.add_argument("--question")
    sp.add_argument("--budget", type=int, help="token target (estimated)")

    sp = add("run", cmd_run, "run records: register, record, import-result, reject-result, usage, show")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("register", help="register an immutable RunSpec and a prepared attempt (starts nothing); "
                                       "records whether code reviews cover its code")
    s.add_argument("--experiment", required=True)
    s.add_argument("--request-id", required=True, help="caller id that makes retries idempotent")
    s.add_argument("--spec", help="RunSpec JSON (inline, @file or path); flags below add to it")
    s.add_argument("--run", help="register a new attempt of this existing run (re-execution)")
    s.add_argument("--arm")
    s.add_argument("--seed", type=int)
    s.add_argument("--code-commit", help="full commit id")
    s.add_argument("--code-digest", help="content digest of the code (rl digest src/)")
    s.add_argument("--code-unknown", action="store_true")
    s.add_argument("--config-digest")
    s.add_argument("--config-ref")
    s.add_argument("--data-revision")
    s.add_argument("--model-revision")
    s.add_argument("--metric")
    s.add_argument("--metric-spec-ref")
    s.add_argument("--environment-ref")
    s.add_argument("--output-spec")
    s.add_argument("--resume-from")
    s.add_argument("--param", action="append", help="k=v (JSON values allowed)")
    s.add_argument("--resource", help="the compute resource this attempt runs on (compute.resources); kept with "
                                      "its planned code and output locations")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--review", action="append", help="code review (RV-x) to check besides the experiment's own")
    g.add_argument("--no-review", metavar="REASON", help="why this run needs no code review (e.g. smoke test)")
    s = ss.add_parser("record", help="record an observation of an attempt (queries nothing)")
    s.add_argument("--observation", help="one observation, a JSON array or JSON Lines (inline, @file, path or -); "
                   "flags fill in or override every record")
    s.add_argument("--attempt")
    s.add_argument("--source", help="tool or person that observed this, e.g. local-shell, qstat")
    s.add_argument("--observed-at", help="when the fact was observed (default: now)")
    s.add_argument("--state", choices=ENUMS["execution_state"])
    s.add_argument("--outcome", choices=ENUMS["outcome"])
    s.add_argument("--raw-state")
    s.add_argument("--source-ref", help="log path or command output the fact comes from")
    s.add_argument("--sequence", type=int, help="the source's own sequence number, when it has one")
    s.add_argument("--record-id", help="stable id for deduplication (default: content digest)")
    s.add_argument("--exec-kind", choices=ENUMS["exec_kind"], default="local_process")
    s.add_argument("--exec-namespace", help="e.g. host:<hostname>:<boot-id> or miyabi-pbs")
    s.add_argument("--exec-id", help="the external tool's full identifier")
    s.add_argument("--exec-locator")
    s.add_argument("--correction-of")
    s.add_argument("--note")
    s = ss.add_parser("import-result", help="import a result manifest (fetches and validates nothing)")
    s.add_argument("--attempt", required=True)
    s.add_argument("--manifest", required=True)
    s.add_argument("--request-id", required=True)
    s = ss.add_parser("reject-result")
    s.add_argument("id")
    s.add_argument("--reason", required=True)
    s = ss.add_parser("usage", help="optional resource usage; unreported is never zero")
    s.add_argument("--accounting-ref", required=True, help="namespaced, e.g. miyabi-pbs:3731052.opbs")
    s.add_argument("--quantity", type=float, required=True)
    s.add_argument("--unit", required=True, help="e.g. s, cpu_h, gpu_h, node_h, usd")
    s.add_argument("--resource-type", required=True, help="e.g. compute, api")
    s.add_argument("--hardware")
    s.add_argument("--basis", choices=ENUMS["usage_basis"], default="reported")
    s.add_argument("--source-ref")
    s.add_argument("--attempts", action="append")
    s.add_argument("--note")
    s.add_argument("--revise", action="store_true")
    s = ss.add_parser("show", help="recorded run state; nothing is queried")
    s.add_argument("id", nargs="?")
    s.add_argument("--active", action="store_true")

    for name, fn, help_ in (("review", cmd_review, "independent reviews by the configured reviewer: code before "
                                                   "costly runs, key conclusions before they are recorded"),
                            ("advise", cmd_advise, "advice rounds: every configured advisor answers on its own; "
                                                   "advice.max_rounds per project")):
        sp = add(name, fn, help_)
        ss = sp.add_subparsers(dest="action", required=True)
        if name == "review":
            s = ss.add_parser("request")
            s.add_argument("--files", action="append", required=True)
            s.add_argument("--author-model", action="append", required=True, help="model(s) that wrote the code")
            s.add_argument("--question", required=True)
            s.add_argument("--target")
            s.add_argument("--configs", action="append")
            s.add_argument("--deps", action="append")
            s.add_argument("--ignore-keys", action="append", help="config keys allowed to vary, e.g. seed,output_dir")
            s.add_argument("--scope")
            s.add_argument("--diff-base")
            s.add_argument("--recheck-of", help="the review this re-checks after fixes (the second of its round)")
            s.add_argument("--new-round", help="start a new review round, with the coordinator's reason")
            s = ss.add_parser("conclusion", help="check a key conclusion (supported/challenged, a retired route, "
                                                 "a formal recommendation) before recording it")
            s.add_argument("--target", action="append", required=True, help="hypothesis id(s) the conclusion is about")
            s.add_argument("--proposal", required=True, help="the conclusion or decision you plan to record")
            s.add_argument("--author-model", action="append", required=True,
                           help="model(s) that drew the conclusion (you, and a researcher that interpreted results)")
            s.add_argument("--question")
            s.add_argument("--recheck-of", help="the review this re-checks after changes (the second of its round)")
            s.add_argument("--new-round", help="start a new review round, with the coordinator's reason")
            s = ss.add_parser("approve", help="the coordinator accepts a review's remaining findings")
            s.add_argument("id")
            s.add_argument("--rationale", required=True)
        else:
            s = ss.add_parser("prepare")
            s.add_argument("--question", required=True)
            s.add_argument("--target")
            s.add_argument("--user-approved", help="the user's approval of a round beyond advice.max_rounds")
        s = ss.add_parser("run")
        if name == "review":
            s.add_argument("id")
        else:
            s.add_argument("id", nargs="+", help="the tickets of a round; several run at the same time")
        s.add_argument("--background", action="store_true")
        s.add_argument("--timeout", type=int, default=1800)
        s.add_argument("--entry", help="override: one model entry <claude|codex|pi>:<model>[@effort]")
        s = ss.add_parser("answer", help="the answer of a reviewer or advisor session opened with rl agent open")
        s.add_argument("id")
        s.add_argument("--answer", required=True, help="file with the answer, ending with its JSON block")
        s.add_argument("--session", help="the session name (default: this session's)")
        s = ss.add_parser("import")
        s.add_argument("id")
        s.add_argument("--answer", required=True)
        s.add_argument("--source", required=True, help="where the answer came from")
        s.add_argument("--model-observed")
        s.add_argument("--provider")
        s.add_argument("--session-id")
        s = ss.add_parser("show")
        s.add_argument("id", nargs="?")

    sp = add("paper", cmd_paper, "literature: add, search, link, access, list")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("add")
    s.add_argument("identifier", nargs="?", help="arXiv id or DOI")
    s.add_argument("--title")
    s.add_argument("--authors", action="append")
    s.add_argument("--year", type=int)
    s.add_argument("--url")
    s.add_argument("--fetch", action="store_true", help="verify metadata with arXiv/Crossref")
    s = ss.add_parser("search")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=5)
    s = ss.add_parser("link")
    s.add_argument("source", help="hypothesis (or experiment/evidence/decision) the paper bears on")
    s.add_argument("paper")
    s.add_argument("--relation", required=True, choices=["nearest_work", "motivated_by", "cites"])
    s.add_argument("--assessment", required=True, choices=PAPER_ASSESSMENTS)
    s.add_argument("--locator", help="section / equation / table / page")
    s.add_argument("--note")
    s.add_argument("--note-file")
    s = ss.add_parser("access")
    s.add_argument("id")
    s.add_argument("--fulltext", required=True, choices=ENUMS["fulltext_access"])
    s.add_argument("--digest")
    ss.add_parser("list")

    sp = add("agent", cmd_agent, "agent sessions in herdr tabs: open, assign, tell, reset, close, switch, list, "
                                 "brief, dialog, answer, check, watch")
    ss = sp.add_subparsers(dest="action", required=True)
    s = ss.add_parser("open", help="open a researcher, reviewer or advisor session in a new herdr tab")
    s.add_argument("name", nargs="?", help="session name, e.g. researcher-1 (default: <role>-<n>)")
    s.add_argument("--role", required=True, choices=agents.ROLE_KINDS)
    s.add_argument("--entry", help="<claude|codex|pi>:<model>[@effort] (default: the first available entry of "
                                   "the ticket or of models.<role>)")
    s.add_argument("--task", help="first assignment of a researcher")
    s.add_argument("--ticket", help="first assignment of a reviewer (RV-x) or an advisor (AD-x)")
    s.add_argument("--no-worktree", action="store_true", help="a researcher works in the project directory")
    s.add_argument("--base", help="where new task branches start (default: the main checkout's branch)")
    s.add_argument("--timeout", type=int, default=90, help="seconds to wait for the agent to start")
    s = ss.add_parser("assign", help="give an open session its next assignment")
    s.add_argument("name")
    s.add_argument("--task")
    s.add_argument("--ticket")
    s = ss.add_parser("tell", help="a recorded message to a role or session; its live session gets a notice")
    s.add_argument("to", help="coordinator or a session name")
    s.add_argument("text", nargs="+")
    s.add_argument("--from", dest="sender", help="default: this session's name, else coordinator")
    s.add_argument("--task")
    s = ss.add_parser("reset", help="prepare a session for reuse")
    s.add_argument("name")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--compact", action="store_true", help="keep a summary of the conversation (/compact)")
    g.add_argument("--clear", action="store_true", help="start a new conversation (/clear, or /new in Codex and pi)")
    s = ss.add_parser("close", help="close the session and its herdr tab")
    s.add_argument("name")
    s.add_argument("--remove-worktree", action="store_true", help="also remove its worktree when it is clean")
    s = ss.add_parser("switch", help="continue a researcher's or reviewer's work on another model (its host's "
                                     "subscription is low): close it and open one of the same name on the next entry")
    s.add_argument("name")
    s.add_argument("--entry", help="<claude|codex|pi>:<model>[@effort] (default: the next entry of its role that is "
                                   "not on a low host)")
    s.add_argument("--now", action="store_true", help="also while it works (it stops mid-turn)")
    s.add_argument("--timeout", type=int, default=90, help="seconds to wait for the new agent to start")
    s = ss.add_parser("brief", help="what the session does now (read by the session itself)")
    s.add_argument("name", nargs="?")
    s = ss.add_parser("list")
    s.add_argument("--all", action="store_true", help="closed sessions too")
    s = ss.add_parser("dialog", help="what a session's agent shows now: a dialog with its options, or a question")
    s.add_argument("name")
    s = ss.add_parser("answer", help="the coordinator answers a dialog in a session: select the option, confirm it, "
                                     "record why")
    s.add_argument("name")
    s.add_argument("option", help="the option's number or the start of its text, as `rl agent dialog` shows it")
    s.add_argument("--reason", required=True, help="why this option (recorded)")
    ss.add_parser("check", help="one watcher round now: restart agents that exited, remind idle sessions of an "
                                "unfinished assignment, report the rest to the coordinator")
    s = ss.add_parser("watch", help="the watcher: a round every agents.watch_minutes over the open sessions (rl "
                                    "agent open starts it in its own herdr tab)")
    s.add_argument("--every", type=float, help="minutes between rounds (default: agents.watch_minutes, 20)")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--start", action="store_true", help="run it in its own herdr tab unless it already runs")
    g.add_argument("--stop", action="store_true", help="stop it and close its tab")
    s.add_argument("--own-tab", action="store_true", help=argparse.SUPPRESS)

    sp = add("delegate", cmd_delegate, "run a ready task in an independent Claude/Codex/pi CLI session")
    sp.add_argument("task")
    sp.add_argument("--entry", help="<claude|codex|pi>:<model>[@effort] (default: the models.researcher entries in "
                                     "order)")
    sp.add_argument("--role", default="researcher")
    sp.add_argument("--timeout", type=int, default=3600)
    sp.add_argument("--background", action="store_true")

    sp = add("hook", cmd_hook, "host hook handler (reads the hook event JSON on stdin)")
    sp.add_argument("event", choices=["session-start"])
    sp.add_argument("--host", required=True, choices=["claude", "codex"])
    _accept_global_options_everywhere(p)
    return p


def _accept_global_options_everywhere(parser: argparse.ArgumentParser) -> None:
    """Let --project and --json also follow a subcommand (`rl decide ... --json`)."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in set(action.choices.values()):
                sub.add_argument("--project", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                sub.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
                _accept_global_options_everywhere(sub)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.fn(args)
    except RLError as exc:
        if args.command == "hook":  # a hook must never break the host session
            print(json.dumps({"systemMessage": f"smooth-research: {exc.message}"}))
            return 0
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, default=str, indent=1), file=sys.stderr)
        for warning in getattr(args, "config_warnings", None) or []:  # they may explain the error
            print(f"warning: {warning}", file=sys.stderr)
        return 3 if exc.code in CONFLICT_CODES else 1
    if getattr(args, "config_warnings", None) or _upgrade_notice(args):
        result["warnings"] = [*(result.get("warnings") or []), *(getattr(args, "config_warnings", None) or []),
                              *_upgrade_notice(args)]
    if args.command == "hook" or args.json:
        print(json.dumps(result, ensure_ascii=False, default=str, indent=None if args.command == "hook" else 1))
    else:
        print(result.get("summary", ""))
        for warning in result.get("warnings") or []:
            print(f"warning: {warning}")
        for step in result.get("next") or []:
            print(f"next: {step}")
    return 0
