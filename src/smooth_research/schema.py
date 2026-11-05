"""Record types, enumerations and relations of the research graph.

Six scientific objects (Hypothesis, Task, Experiment, Evidence, Decision, Paper) are graph nodes; documents,
runs, attempts, execution references, observations, results, usage, sessions, messages, tickets and events are
auxiliary records in the same database with explicit fields, not one free-form JSON blob.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The record format. A change that older records do not already satisfy (a field every record of a type is read
# with, a renamed field or enum value) raises it and adds a migration to upgrade.MIGRATIONS; tests open a frozen
# project of every earlier version (tests/fixtures/projects/).
SCHEMA_VERSION = 4

ENUMS: dict[str, tuple[str, ...]] = {
    "evidence_status": ("untested", "supported", "challenged", "inconclusive"),
    "disposition": ("candidate", "active", "paused", "retired"),
    "task_state": ("ready", "active", "waiting", "blocked", "delivered", "accepted", "stopped"),
    "task_kind": ("framework", "implement", "experiment", "analysis", "literature", "review", "advice", "other"),
    "stage": ("E0", "E1", "E2", "E3"),
    "execution_state": ("prepared", "queued", "running", "ended", "unknown"),
    "outcome": ("success", "failed", "cancelled", "unknown"),
    "result_status": ("absent", "partial", "complete", "rejected"),
    "validity": ("pending", "valid", "invalid"),
    "evidence_level": ("diagnostic", "screening", "exploratory", "confirmatory"),
    "decision_action": ("continue", "expand", "modify", "diagnose", "consult", "pause", "stop", "reopen",
                        "recommend_formal", "continue_probe"),
    "decision_kind": ("route", "stage"),
    "message_type": ("TASK", "RESULT", "WAIT", "BLOCKED", "HANDOFF", "CANCEL", "NOTE"),
    "blocked_reason": ("engineering", "model_unavailable", "budget", "science"),
    "ticket_kind": ("review", "advice"),
    "review_subject": ("code", "conclusion"),
    "ticket_status": ("pending", "running", "answered", "failed", "unavailable"),
    "exec_kind": ("local_process", "remote_process", "container", "hpc_job", "external"),
    "usage_basis": ("measured", "estimated", "reported"),
    "metadata_status": ("unverified", "verified", "failed"),
    "fulltext_access": ("unknown", "available", "unavailable"),
    "session_status": ("active", "ended", "superseded"),
    "severity": ("none", "low", "medium", "high"),
}


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
       statement_ref=F(), evidence_status=F(required=True, enum="evidence_status"),
       disposition=F(required=True, enum="disposition"), revision=F("int", True), needs_recheck=F("bool"),
       recheck_reason=F(), origin=F(), label=F(required=True)),
    _e("Experiment", "E", graph=True, title=F(required=True), question=F(required=True),
       stage=F(enum="stage"), contract_path=F(), contract_ref=F(), revision=F("int", True),
       revised_after_results=F("bool"), code_origin=F(), label=F(required=True)),
    _e("Evidence", "EV", graph=True, summary=F(required=True), validity=F(required=True, enum="validity"),
       evidence_level=F(required=True, enum="evidence_level"), scope=F(), artifact_refs=F("json"), doc_ref=F(),
       validity_reason=F()),
    _e("Decision", "D", graph=True, kind=F(required=True, enum="decision_kind"),
       action=F(required=True, enum="decision_action"), rationale=F(required=True), rationale_ref=F(),
       reopen_condition=F(), implementation_ready=F(), evidence_scope=F(), needs_recheck=F("bool"),
       recheck_reason=F(), evidence_status_set=F(enum="evidence_status")),
    _e("Paper", "P", graph=True, title=F(required=True), authors=F("json"), year=F("int"), doi=F(), arxiv_id=F(),
       arxiv_version=F(), url=F(), metadata_status=F(required=True, enum="metadata_status"),
       fulltext_access=F(required=True, enum="fulltext_access"), metadata_source=F(), fetched_at=F(),
       content_digest=F(), note_refs=F("json")),
    _e("Task", "T", graph=True, title=F(required=True), kind=F(required=True, enum="task_kind"),
       state=F(required=True, enum="task_state"), to_role=F(), owner_role=F(), owner_session=F(),
       request_ref=F(), output_refs=F("json"), handoff_ref=F(), next_action=F(),
       blocked_reason=F(enum="blocked_reason"), wait_attempts=F("json"), note=F(), revision=F("int", True),
       branch=F()),
    _e("Document", "DOC", path=F(required=True), doc_type=F(required=True), latest_revision=F("int", True),
       latest_digest=F(required=True)),
    _e("DocRevision", "DOC", document_id=F(required=True), revision=F("int", True), digest=F(required=True),
       object_path=F(required=True), source_path=F(required=True), git_commit=F(), size=F("int")),
    _e("Session", "S", role_id=F(required=True), host=F(required=True), host_session_id=F(),
       model_requested=F(), model_observed=F(), effort=F(), worktree=F(),
       status=F(required=True, enum="session_status"), ended_at=F(), role_kind=F(), entry=F(), cwd=F(),
       branch=F(), assignment=F(), herdr_agent=F(), herdr_tab=F(), herdr_pane=F(), watch=F("json")),
    _e("Message", "MSG", seq=F("int", True), type=F(required=True, enum="message_type"), task_id=F(),
       from_role=F(required=True), to_role=F(required=True), document_refs=F("json"), base_revision=F("int"),
       in_reply_to=F(), next_action=F(), text=F(), digest=F(required=True)),
    _e("Cursor", "CUR", role=F(required=True), position=F("int", True)),
    _e("Run", "RUN", experiment_id=F(required=True), experiment_ref=F(required=True), request_id=F(required=True),
       spec=F("json", True), spec_digest=F(required=True), attempt_count=F("int", True)),
    _e("Attempt", "ATT", run_id=F(required=True), index=F("int", True), request_id=F(),
       execution_state=F(required=True, enum="execution_state"), outcome=F(required=True, enum="outcome"),
       result_status=F(required=True, enum="result_status"), last_trusted_state=F(), last_trusted_at=F(),
       last_observed_at=F(), last_record_id=F(), last_source=F(), last_sequence=F("int"), closed_at=F(),
       needs_check=F("bool"), issues=F("json"), code_review=F("json"), resource=F("json")),
    _e("ExecRef", "X", kind=F(required=True, enum="exec_kind"), executor_namespace=F(required=True),
       opaque_id=F(required=True), locator=F()),
    _e("Observation", "OBS", attempt_id=F(required=True), source=F(required=True), observed_at=F(required=True),
       received_at=F(required=True), source_sequence=F("int"), execution_state=F(required=True, enum="execution_state"),
       outcome=F(required=True, enum="outcome"), raw_state=F(), source_ref=F(), occurred_at=F(), correction_of=F(),
       note=F(), content_digest=F(required=True), effect=F(required=True)),
    _e("Result", "RES", attempt_id=F(required=True), request_id=F(required=True), revision=F("int", True),
       status=F(required=True, enum="result_status"), manifest=F("json", True), manifest_digest=F(required=True),
       spec_match=F("bool"), mismatches=F("json"), verification=F("json"), verification_scope=F(),
       usable_for_planned_comparison=F("bool"), rejected_reason=F()),
    _e("Usage", "U", accounting_ref=F(required=True), quantity=F("float", True), unit=F(required=True),
       resource_type=F(required=True), hardware=F(), basis=F(required=True, enum="usage_basis"), source_ref=F(),
       revision=F("int", True), superseded=F("bool"), content_digest=F(required=True), note=F()),
    _e("Ticket", "RV", kind=F(required=True, enum="ticket_kind"), status=F(required=True, enum="ticket_status"),
       target_id=F(), question=F(), dedupe_key=F(required=True), request_ref=F(), answer_ref=F(),
       candidates=F("json"), requested_entry=F(), requested_model=F(), observed_model=F(), provider=F(),
       host_session_id=F(), author_models=F("json"), relation=F(), verdict=F(), summary=F(), usage=F("json"),
       cost_usd=F("float"), error=F(), calls=F("json"), reused_from=F(), recheck_of=F(), answered_at=F(),
       subject=F(enum="review_subject"), code_scope=F("json"), advice_round=F("int"), user_approved=F(),
       round_of=F(), round_index=F("int"), round_reason=F(), requested_by=F(), assigned_session=F(), answered_by=F(),
       open_severity=F(enum="severity"), escalated=F("bool"), approved=F(), approved_at=F(),
       earlier_answers=F("json"), code_checkout=F(), changed_during_review=F("json")),
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


ANY_GRAPH = GRAPH_LABELS + ("Ticket",)

RELATIONS: dict[str, Relation] = {
    "derived_from": Relation("DERIVED_FROM", ("Hypothesis",), ("Hypothesis", "Ticket", "Paper"), acyclic=True),
    "combines": Relation("COMBINES", ("Hypothesis",), ("Hypothesis",)),
    "tests": Relation("TESTS", ("Experiment",), ("Hypothesis",), revision_bound=True),
    "supports": Relation("SUPPORTS", ("Evidence",), ("Hypothesis",), revision_bound=True),
    "challenges": Relation("CHALLENGES", ("Evidence",), ("Hypothesis",), revision_bound=True),
    "depends_on": Relation("DEPENDS_ON", ("Task", "Experiment"), ("Task", "Experiment"), acyclic=True),
    "compared_with": Relation("COMPARED_WITH", ("Experiment",), ("Experiment",)),
    "code_from": Relation("CODE_FROM", ("Experiment",), ("Experiment",), acyclic=True),
    "motivated_by": Relation("MOTIVATED_BY", ("Hypothesis", "Experiment"), ("Paper", "Ticket", "Evidence", "Decision")),
    "nearest_work": Relation("NEAREST_WORK", ("Hypothesis",), ("Paper",), revision_bound=True),
    "cites": Relation("CITES", ("Hypothesis", "Experiment", "Evidence", "Decision"), ("Paper",), revision_bound=True),
    "based_on": Relation("BASED_ON", ("Decision",), ("Evidence", "Paper", "Ticket", "Result", "Experiment")),
    "decides": Relation("DECIDES", ("Decision",), ("Hypothesis", "Experiment", "Task"), revision_bound=True),
    "supersedes": Relation("SUPERSEDES", ("Hypothesis", "Experiment", "Decision", "Evidence", "Result", "Usage"),
                           ("Hypothesis", "Experiment", "Decision", "Evidence", "Result", "Usage"), acyclic=True),
    "targets": Relation("TARGETS", ("Task", "Ticket"), ANY_GRAPH),
    "about": Relation("ABOUT", ("Evidence",), ("Experiment",)),
    "from_attempt": Relation("FROM_ATTEMPT", ("Evidence",), ("Attempt",)),
    "from_result": Relation("FROM_RESULT", ("Evidence",), ("Result",)),
    "of_experiment": Relation("OF_EXPERIMENT", ("Run",), ("Experiment",)),
    "of_run": Relation("OF_RUN", ("Attempt",), ("Run",)),
    "bound_to": Relation("BOUND_TO", ("Attempt",), ("ExecRef",)),
    "covers": Relation("COVERS", ("Usage",), ("Attempt",)),
}

# Relations an agent may create directly with `rl link`; the rest are written by their own commands.
LINKABLE = ("derived_from", "combines", "tests", "supports", "challenges", "depends_on", "compared_with",
            "code_from", "motivated_by", "nearest_work", "cites", "based_on", "supersedes")

PAPER_ASSESSMENTS = ("search_hit", "metadata_checked", "fulltext_read", "checked")
