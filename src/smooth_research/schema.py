"""Record types, enumerations and relations of the research graph (schema 5).

Hypotheses, experiments, evidence, decisions, tasks, papers and phases are graph nodes; documents, runs, attempts,
execution references, observations, results, usage, sessions, dialogue entries, asks, tickets and events are
auxiliary records in the same database with explicit fields.

Who waits on a record is not a field: the rules table (inbox.py) computes it from the records' states.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The record format. A change that older records do not already satisfy raises it and adds a migration to
# upgrade.MIGRATIONS; tests open the frozen project of every supported earlier version (tests/fixtures/projects/).
SCHEMA_VERSION = 5
# Projects made before the 2026-11 redesign (schema < 5) are not opened: they continue with the plugin that made
# them (design 10.4).
MIN_SCHEMA_VERSION = 5

ENUMS: dict[str, tuple[str, ...]] = {
    "evidence_status": ("untested", "supported", "challenged", "inconclusive"),
    "disposition": ("candidate", "active", "paused", "retired"),
    "task_kind": ("tree", "hypothesis", "phase", "prep", "execute"),
    # queued: made by the coordinator, not released; open: released or made, nobody holds it; active: held;
    # blocked: its researcher waits on the scientist; delivered: waits for acceptance (or a merge conflict);
    # merging: the acceptance test job runs; accepted: merged; cancelled
    "task_state": ("queued", "open", "active", "blocked", "delivered", "merging", "accepted", "cancelled"),
    "merge_state": ("merging", "merged", "conflict"),
    "block_reason": ("design", "mandate", "question"),
    "run_category": ("trial", "prep", "evidence"),
    "decision_kind": ("verdict", "stop", "route", "phase"),
    "decision_state": ("draft", "final"),
    "ask_kind": ("decision", "approval", "report"),
    "ask_state": ("open", "forwarded", "answered", "closed"),
    "answerer": ("coordinator", "user"),
    "ticket_kind": ("review", "advice"),
    "review_kind": ("design", "conclusion", "tree"),
    "advice_kind": ("consult", "ruling"),
    # waiting: the reviewer or advisor works on it; answered: a verdict is in, the author acts; passed: closed with a
    # pass; escalated: out of passes, a ruling or the scientist decides; closed: advice given or round abandoned
    "ticket_status": ("waiting", "answered", "passed", "escalated", "closed"),
    "segment": ("scientist", "researcher"),
    "finding_category": ("method", "engineering"),
    "finding_status": ("draft", "open", "fixed", "constrained", "withdrawn", "ruled"),
    "severity": ("low", "medium", "high"),
    "session_status": ("open", "ended"),
    "message_kind": ("question", "answer", "discussion", "consult"),
    "execution_state": ("prepared", "queued", "running", "ended", "unknown"),
    "outcome": ("success", "failed", "cancelled", "unknown"),
    "result_status": ("absent", "partial", "complete", "rejected"),
    "validity": ("pending", "valid", "invalid"),
    "evidence_level": ("diagnostic", "screening", "exploratory", "confirmatory"),
    "exec_kind": ("local_process", "remote_process", "container", "hpc_job", "external"),
    "usage_basis": ("measured", "estimated", "reported"),
    "metadata_status": ("unverified", "verified", "failed"),
    "fulltext_access": ("unknown", "available", "unavailable"),
    # documents attached to records; no handoff or notes type (design chapter 12)
    "doc_type": ("plan", "constraints", "brief", "mandate", "analysis", "rationale", "answer", "advice",
                 "phase_conclusion", "tree_rationale", "output", "literature"),
}

FINDING_CLOSED = ("fixed", "constrained", "withdrawn", "ruled")
TASK_OPEN = ("queued", "open", "active", "blocked", "delivered", "merging")
TASK_DONE = ("accepted", "cancelled")


@dataclass(frozen=True)
class F:
    type: str = "str"  # str | int | float | bool | json
    required: bool = False
    enum: str | None = None


@dataclass(frozen=True)
class Entity:
    label: str
    prefix: str
    fields: dict[str, F] = field(default_factory=dict)
    graph: bool = False
    width: int = 4


COMMON = {"id": F(required=True), "created_at": F(), "updated_at": F()}


def _e(entity: str, prefix: str, graph: bool = False, width: int = 4, **fields: F) -> Entity:
    return Entity(entity, prefix, {**COMMON, **fields}, graph, width)


ENTITIES: dict[str, Entity] = {e.label: e for e in [
    _e("Hypothesis", "H", graph=True, title=F(required=True), statement=F(required=True), scope=F(),
       source=F(), purpose=F(required=True), evidence_status=F(required=True, enum="evidence_status"),
       disposition=F(required=True, enum="disposition"), revision=F("int", True), label=F(required=True),
       composite=F("bool"), cost=F(), flag=F("json"), defer_reason=F(), origin=F(), needs_recheck=F("bool"),
       recheck_reason=F()),
    _e("Experiment", "E", graph=True, title=F(required=True), question=F(required=True), label=F(required=True),
       revision=F("int", True), revision_reason=F(), brief_ref=F(), manifest_path=F(), manifest_digest=F(),
       manifest=F("json"), protected=F("json"), semantic_tests=F(), design_commit=F(), sealed_at=F(),
       sealed_by=F(), estimate=F()),
    _e("Evidence", "EV", graph=True, summary=F(required=True), validity=F(required=True, enum="validity"),
       evidence_level=F(required=True, enum="evidence_level"), scope=F(), artifact_refs=F("json"), doc_ref=F(),
       validity_reason=F(), task_id=F(), needs_recheck=F("bool"), recheck_reason=F()),
    _e("Decision", "D", graph=True, kind=F(required=True, enum="decision_kind"),
       state=F(required=True, enum="decision_state"), rationale=F(required=True), rationale_ref=F(),
       evidence_status_set=F(enum="evidence_status"), disposition_set=F(enum="disposition"), did=F(), means=F(),
       highlights=F("json"), supports=F("json"), challenges=F("json"), task_id=F(), author=F(), final_at=F(),
       needs_recheck=F("bool"), recheck_reason=F()),
    _e("Paper", "P", graph=True, title=F(required=True), authors=F("json"), year=F("int"), doi=F(), arxiv_id=F(),
       arxiv_version=F(), url=F(), metadata_status=F(required=True, enum="metadata_status"),
       fulltext_access=F(required=True, enum="fulltext_access"), metadata_source=F(), fetched_at=F(),
       content_digest=F(), note_refs=F("json")),
    _e("Task", "T", graph=True, title=F(required=True), kind=F(required=True, enum="task_kind"),
       state=F(required=True, enum="task_state"), revision=F("int", True), creator=F(required=True),
       holder=F(), holders=F("json"), priority=F("int"), envelope=F(), parallel_limit=F("int"), reason=F(),
       cancel_reason=F(), conclusion=F(), limitations=F(), block_reason=F(enum="block_reason"), block_note=F(),
       answer_note=F(), return_note=F(), branch=F(), worktree=F(), design_commit=F(), delivered_commit=F(),
       merge_state=F(enum="merge_state"), merge_note=F(), merge_commit=F(), requirement=F(), done_check=F(),
       mandate_ref=F(), estimate=F(), released_at=F(), delivered_at=F(), outputs=F("json"), tests=F("json"),
       fast_lane=F("bool"), scientist_session=F(), dispatch_note=F(), state_note=F(), sealed_design=F("json")),
    _e("Phase", "PH", graph=True, title=F(required=True), budget=F(), scientist_limit=F("int", True)),
    _e("Ask", "RQ", kind=F(required=True, enum="ask_kind"), task_id=F(required=True),
       state=F(required=True, enum="ask_state"), by=F(required=True), stuck=F(required=True), options=F(),
       recommend=F(), cost=F(), answer=F(), answered_by=F(enum="answerer"), answered_at=F(), forwarded_at=F()),
    _e("Document", "DOC", path=F(required=True), doc_type=F(required=True, enum="doc_type"),
       owner=F(required=True), latest_revision=F("int", True), latest_digest=F(required=True)),
    _e("DocRevision", "DOC", document_id=F(required=True), revision=F("int", True), digest=F(required=True),
       object_path=F(required=True), source_path=F(required=True), git_commit=F(), size=F("int")),
    _e("Session", "S", name=F(required=True), role=F(required=True), subject=F(required=True),
       host=F(required=True), entry=F(), model=F(), effort=F(), status=F(required=True, enum="session_status"),
       herdr_pane=F(), herdr_tab=F(), host_session_id=F(), cwd=F(), ended_at=F(), last_prompt_at=F(),
       last_seq=F("int"), last_user_at=F(), last_active_at=F(), conversations=F("int"), needs_decision=F(),
       context_tokens=F("int"), told=F("json")),
    _e("Message", "MSG", kind=F(required=True, enum="message_kind"), subject=F(required=True),
       from_role=F(required=True), to_role=F(required=True), text=F(required=True), refs=F("json"),
       in_reply_to=F(), finding=F(), closed_at=F()),
    _e("Run", "RUN", task_id=F(required=True), experiment_id=F(), category=F(required=True, enum="run_category"),
       arm=F(), seed=F(), deviates=F("bool"), deviation=F(), request_id=F(required=True), spec=F("json", True),
       spec_digest=F(required=True), attempt_count=F("int", True), code_commit=F(), minutes=F("float"),
       purpose=F(), ticket_id=F()),
    _e("Attempt", "ATT", run_id=F(required=True), index=F("int", True), request_id=F(),
       execution_state=F(required=True, enum="execution_state"), outcome=F(required=True, enum="outcome"),
       result_status=F(required=True, enum="result_status"), last_trusted_state=F(), last_trusted_at=F(),
       last_observed_at=F(), last_record_id=F(), last_source=F(), last_sequence=F("int"), closed_at=F(),
       needs_check=F("bool"), issues=F("json"), resource=F("json"), expected_end=F(), submitted_by=F(),
       ended_note=F(), handled_at=F()),
    _e("ExecRef", "X", kind=F(required=True, enum="exec_kind"), executor_namespace=F(required=True),
       opaque_id=F(required=True), locator=F()),
    _e("Observation", "OBS", attempt_id=F(required=True), source=F(required=True), observed_at=F(required=True),
       received_at=F(required=True), source_sequence=F("int"), execution_state=F(required=True, enum="execution_state"),
       outcome=F(required=True, enum="outcome"), raw_state=F(), source_ref=F(), occurred_at=F(), correction_of=F(),
       note=F(), content_digest=F(required=True), effect=F(required=True)),
    _e("Result", "RES", attempt_id=F(required=True), request_id=F(required=True), revision=F("int", True),
       status=F(required=True, enum="result_status"), manifest=F("json", True), manifest_digest=F(required=True),
       spec_match=F("bool"), mismatches=F("json"), verification=F("json"), verification_scope=F(),
       usable=F("bool"), rejected_reason=F()),
    _e("Usage", "U", accounting_ref=F(required=True), quantity=F("float", True), unit=F(required=True),
       resource_type=F(required=True), hardware=F(), basis=F(required=True, enum="usage_basis"), source_ref=F(),
       revision=F("int", True), superseded=F("bool"), content_digest=F(required=True), note=F()),
    _e("Ticket", "RV", kind=F(required=True, enum="ticket_kind"), status=F(required=True, enum="ticket_status"),
       review_kind=F(enum="review_kind"), advice_kind=F(enum="advice_kind"), task_id=F(), target_id=F(),
       segment=F(enum="segment"), passes=F("json"), findings=F("json"), requested_by=F(required=True),
       question=F(), answer_ref=F(), ruling_of=F(), ruled=F("json"), verdict=F(), answered_at=F(),
       entry=F(), model=F()),
    _e("Event", "EVT", width=6, seq=F("int", True), type=F(required=True), entity_id=F(), actor=F(), summary=F()),
    _e("Request", "REQ", op=F(required=True), request_id=F(required=True), digest=F(required=True),
       result=F("json", True)),
]}

# Ticket ids use RV (review) or AD (advice); both are the Ticket label.
PREFIX_LABEL = {e.prefix: e.label for e in ENTITIES.values() if e.label not in ("DocRevision", "Ticket")}
PREFIX_LABEL.update({"RV": "Ticket", "AD": "Ticket"})

GRAPH_LABELS = tuple(e.label for e in ENTITIES.values() if e.graph)


@dataclass(frozen=True)
class Relation:
    edge: str
    sources: tuple[str, ...]
    targets: tuple[str, ...]
    acyclic: bool = False
    revision_bound: bool = False  # the edge records the target's revision at link time


RELATIONS: dict[str, Relation] = {
    # the primary parent edge carries primary=true; primary edges make the hypothesis tree
    "derived_from": Relation("DERIVED_FROM", ("Hypothesis",), ("Hypothesis",), acyclic=True),
    "assumes": Relation("ASSUMES", ("Hypothesis",), ("Hypothesis",), acyclic=True),
    "tests": Relation("TESTS", ("Experiment",), ("Hypothesis",), revision_bound=True),
    "supports": Relation("SUPPORTS", ("Evidence",), ("Hypothesis",), revision_bound=True),
    "challenges": Relation("CHALLENGES", ("Evidence",), ("Hypothesis",), revision_bound=True),
    "depends_on": Relation("DEPENDS_ON", ("Task",), ("Task",), acyclic=True),
    "motivated_by": Relation("MOTIVATED_BY", ("Hypothesis", "Experiment"), ("Paper", "Decision")),
    "nearest_work": Relation("NEAREST_WORK", ("Hypothesis",), ("Paper",), revision_bound=True),
    "cites": Relation("CITES", ("Hypothesis", "Experiment", "Evidence", "Decision"), ("Paper",), revision_bound=True),
    "based_on": Relation("BASED_ON", ("Decision",), ("Evidence", "Paper", "Ticket", "Result", "Experiment",
                                                     "Decision", "Task"), acyclic=True),
    "decides": Relation("DECIDES", ("Decision",), ("Hypothesis", "Experiment", "Phase"), revision_bound=True),
    "supersedes": Relation("SUPERSEDES", ("Hypothesis", "Experiment", "Decision", "Evidence", "Result", "Usage"),
                           ("Hypothesis", "Experiment", "Decision", "Evidence", "Result", "Usage"), acyclic=True),
    "targets": Relation("TARGETS", ("Task",), ("Hypothesis", "Experiment", "Phase")),
    "part_of": Relation("PART_OF", ("Task",), ("Task",), acyclic=True),
    "in_phase": Relation("IN_PHASE", ("Task",), ("Phase",)),
    "about": Relation("ABOUT", ("Evidence",), ("Experiment",)),
    "from_attempt": Relation("FROM_ATTEMPT", ("Evidence",), ("Attempt",)),
    "from_result": Relation("FROM_RESULT", ("Evidence",), ("Result",)),
    "of_experiment": Relation("OF_EXPERIMENT", ("Run",), ("Experiment",)),
    "of_task": Relation("OF_TASK", ("Run",), ("Task",)),
    "of_run": Relation("OF_RUN", ("Attempt",), ("Run",)),
    "bound_to": Relation("BOUND_TO", ("Attempt",), ("ExecRef",)),
    "covers": Relation("COVERS", ("Usage",), ("Attempt",)),
}

# Relations an agent may create directly with `rl link`; the rest are written by their own commands.
LINKABLE = ("assumes", "depends_on", "motivated_by", "cites", "based_on", "supersedes")

PAPER_ASSESSMENTS = ("search_hit", "metadata_checked", "fulltext_read", "checked")

# Session roles (design chapter 3). The user is not a session; "user" is a recipient of to-do items only.
ROLES = ("coordinator", "scientist", "researcher", "reviewer", "advisor")
