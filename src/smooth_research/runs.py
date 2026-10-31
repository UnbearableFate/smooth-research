"""Platform-independent run records: immutable RunSpecs, attempts, external references, observations,
results and optional usage.

Nothing here starts, polls, retries, cancels, downloads or validates external work. Executors run
experiments with their own tools and hand the facts to register / record / import-result.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .project import Project
from .research import brief, prepare_contract, recheck_dependents, seal_contract
from .schema import ENUMS
from .store import Store, Tx
from .util import RLError, digest_file, digest_json, now_utc, parse_time, short

SPEC_FIELDS = {"arm", "seed", "replicate", "code_ref", "config_digest", "config_ref", "data_revision",
               "model_revision", "metric_spec_ref", "metric", "environment_ref", "output_spec", "params",
               "resume_from", "notes"}
SPEC_REQUIRED = (("arm",), ("code_ref",), ("config_digest", "config_ref"), ("data_revision",),
                 ("metric_spec_ref", "metric"), ("output_spec",))
COMPARED = ("config_digest", "config_ref", "data_revision", "model_revision", "metric_spec_ref", "metric",
            "environment_ref")
FULL_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
OBS_FIELDS = {"record_id", "attempt_id", "source", "observed_at", "occurred_at", "source_sequence", "execution_state",
              "outcome", "raw_state", "source_ref", "execution_ref", "usage", "correction_of", "note"}
STALE_AFTER_H = 6.0


# -- register -------------------------------------------------------------------------------------------------


def normalize_spec(spec: dict) -> dict:
    unknown = set(spec) - SPEC_FIELDS
    if unknown:
        raise RLError("INVALID", f"unknown RunSpec fields {sorted(unknown)}; allowed: {sorted(SPEC_FIELDS)} "
                                 "(experiment_ref and protocol_ref are filled by the tool)")
    missing = [" or ".join(alts) for alts in SPEC_REQUIRED if all(spec.get(a) in (None, "") for a in alts)]
    if missing:
        raise RLError("INVALID", f"RunSpec needs {', '.join(missing)}; write \"unknown\" for facts you do not "
                                 "have instead of guessing")
    code = spec["code_ref"]
    if code != "unknown":
        if not isinstance(code, dict) or not (code.get("commit") or code.get("content_digest")):
            raise RLError("INVALID", "code_ref is {commit: <full id>} and/or {content_digest: <algo:hex>}, or \"unknown\"")
        commit = code.get("commit")
        if commit and not FULL_COMMIT.match(str(commit)):
            raise RLError("INVALID", f"code_ref.commit {commit!r} is not a full commit id; branch names, HEAD and "
                                     "short ids are not immutable versions")
        if code.get("content_digest") and ":" not in str(code["content_digest"]):
            raise RLError("INVALID", "code_ref.content_digest must name its algorithm, e.g. blake2b:<hex>")
    return spec


def register(store: Store, experiment: str, spec: dict, request_id: str, run_id: str | None = None,
             reviews: list[str] = (), no_review: str | None = None) -> dict:
    from .reviews import code_coverage  # reviews imports this module (through context and view)
    spec = normalize_spec(dict(spec))
    payload = digest_json({"experiment": experiment, "spec": spec, "run": run_id})
    prep = prepare_contract(store, experiment)  # a registered run is a formal reference: seal the contract
    check = code_coverage(store, experiment, spec["code_ref"], list(reviews), no_review)
    with store.write() as tx:
        previous = tx.request("run.register", request_id, payload)
        if previous:
            return {**previous, "duplicate": True,
                    "summary": f"duplicate request {request_id}: {previous['run']} {previous['attempt']} (no new record)"}
        if run_id:
            run = tx.require(run_id, "Run")
            if run["experiment_id"] != experiment:
                raise RLError("INVALID", f"{run_id} belongs to {run['experiment_id']}, not {experiment}")
            stored = {k: v for k, v in run["spec"].items() if k not in ("experiment_ref", "protocol_ref")}
            if digest_json(stored) != digest_json(spec):
                raise RLError("CONFLICT", f"the spec differs from {run_id}'s immutable RunSpec; a scientific change "
                                          "is a new run (omit --run)")
            index = run["attempt_count"] + 1
            tx.update(run_id, {"attempt_count": index})
            spec_digest = run["spec_digest"]
        else:
            exp_ref = seal_contract(tx, experiment, prep, store.project)
            exp = tx.require(experiment)
            full = {"experiment_ref": exp_ref, "protocol_ref": exp["contract_ref"], **spec}
            spec_digest = digest_json(full)
            run_id, index = tx.next_id("Run"), 1
            tx.create("Run", {"id": run_id, "experiment_id": experiment, "experiment_ref": exp_ref,
                              "request_id": request_id, "spec": full, "spec_digest": spec_digest, "attempt_count": 1})
            tx.link("of_experiment", run_id, experiment)
        attempt = f"ATT-{run_id.split('-')[1]}-{index}"
        tx.create("Attempt", {"id": attempt, "run_id": run_id, "index": index, "request_id": request_id,
                              "execution_state": "prepared", "outcome": "unknown", "result_status": "absent",
                              "issues": [], "code_review": check})
        tx.link("of_run", attempt, run_id)
        result = {"run": run_id, "attempt": attempt, "spec_digest": spec_digest, "code_review": check}
        tx.remember("run.register", request_id, payload, result)
        tx.event("run.register", attempt, f"{run_id} attempt {index} prepared")
    unknown = sorted(k for k, v in spec.items() if v == "unknown")
    source = f" ({check['source']})" if check.get("source") else ""
    return {**result, "summary": f"{run_id} {attempt} prepared (spec {short(spec_digest)})"
                                 + (f"; unknown: {', '.join(unknown)}" if unknown else "")
                                 + ". Nothing was started: run it with your own tool, then `rl run record`."
                                 + f"\ncode review{source}: {code_review_note(check)}{_review_hint(check)}"}


def _review_hint(check: dict) -> str:
    if check["status"] == "stale":
        last = max(i for ids in check["changed"].values() for i in ids)
        return f". Before a costly run, re-check the changed files: rl review request ... --recheck-of {last}"
    if check["status"] in ("none", "not_passed"):
        return (". A costly run on a new core path needs a code review first; for other runs say why "
                "with --no-review \"<reason>\"")
    return ""


def code_review_note(check: dict | None, tx: Tx | None = None) -> str:
    """The code review check recorded when an attempt was registered, in one line. With tx it also names the
    reviews that did not pass then and that the coordinator has approved since (the recorded check stays)."""
    if not check:
        return "not checked"  # registered before the check existed
    status = check["status"]
    if status == "waived":
        text = f"not needed: {check['reason']}"
    elif status == "covered":
        text = f"covered by {', '.join(check['reviews'])}"
    elif status == "stale":
        paths = [f"{p} changed since {', '.join(ids)}" for p, ids in check["changed"].items()]
        text = "stale: " + "; ".join(paths[:3]) + (f"; {len(paths) - 3} more" if len(paths) > 3 else "")
    elif status == "unchecked":
        text = f"unchecked: {check['reason']}"
    elif status == "not_passed":
        text = f"no passing review ({', '.join(check['open'])})"
    else:
        text = "not reviewed"
    if status in ("covered", "stale") and check.get("open"):
        text += f" ({', '.join(check['open'])})"
    if tx is not None and (later := _approved_since(tx, check)):
        text += f"; approved by the coordinator since: {', '.join(later)}"
    return text


def _approved_since(tx: Tx, check: dict) -> list[str]:
    marks = [*check.get("open", []), *check.get("reviews", [])]
    ids = [m.split()[0] for m in marks if "approved" not in m]
    return [i for i in ids if (t := tx.get(i, "Ticket")) and t.get("approved")]


def evidence_attempts(tx: Tx, ev_id: str) -> list[dict]:
    """The attempts behind an evidence record: linked directly or through its results."""
    ids: list[str] = []
    for e in tx.out(ev_id):
        if e["rel"] == "from_attempt":
            aid = e["id"]
        elif e["rel"] == "from_result":
            aid = tx.require(e["id"])["attempt_id"]
        else:
            continue
        if aid not in ids:
            ids.append(aid)
    return [tx.require(a) for a in ids]


def code_review_summary(tx: Tx, attempts: list[dict]) -> str:
    """Attempts grouped by the code review check recorded at their registration."""
    groups: dict[str, list[str]] = {}
    for att in attempts:
        groups.setdefault(code_review_note(att.get("code_review"), tx), []).append(att["id"])
    return "; ".join(f"{', '.join(ids)} {note}" for note, ids in groups.items())


# -- record ---------------------------------------------------------------------------------------------------


def _newer(att: dict, obs: dict) -> bool:
    """Order by the source's own sequence when both come from one source, otherwise by observed_at."""
    if not att.get("last_observed_at"):
        return True
    if obs.get("source_sequence") is not None and att.get("last_sequence") is not None \
            and obs["source"] == att.get("last_source"):
        return obs["source_sequence"] > att["last_sequence"]
    return parse_time(obs["observed_at"]) >= parse_time(att["last_observed_at"])


def _project(att: dict, obs: dict, received: str) -> tuple[str, dict, list[str]]:
    """Derive the attempt's state from one more observation: (effect, changes, new issues)."""
    state, outcome = obs["execution_state"], obs["outcome"]
    newer = _newer(att, obs)
    applied = {"last_observed_at": obs["observed_at"], "last_record_id": obs["record_id"],
               "last_source": obs["source"], "last_sequence": obs.get("source_sequence")}
    if obs.get("correction_of"):
        changes = {**applied, "execution_state": state, "outcome": outcome, "needs_check": False,
                   "closed_at": received if state == "ended" else None}
        if state not in ("unknown",):
            changes.update(last_trusted_state=state, last_trusted_at=obs["observed_at"])
        return "correction", changes, [f"corrected by {obs['record_id']}: {obs.get('note')}"]
    if att["execution_state"] == "ended":
        if state == "ended":
            if outcome in (att["outcome"], "unknown"):
                return "confirming", {}, []
            if att["outcome"] == "unknown":
                return "applied", {**applied, "outcome": outcome}, []
            return "conflict", {"needs_check": True}, [
                f"{obs['record_id']} reports outcome {outcome}; recorded outcome {att['outcome']} kept — verify and "
                "record an explicit correction if needed"]
        if state == "unknown":
            return "observation_issue", {}, [f"{obs['record_id']}: lookup gave no state ({obs.get('raw_state') or 'unknown'}); "
                                             "the recorded end state stands"]
        if newer:
            return "conflict", {"needs_check": True}, [
                f"{obs['record_id']} reports {state} after the recorded end — verify with the execution tool"]
        return "stale", {}, []
    if not newer:
        if state == "ended":
            return "conflict", {"needs_check": True}, [
                f"{obs['record_id']} reports ended at {obs['observed_at']}, earlier than the last observation; "
                "order unclear — verify"]
        return "stale", {}, []
    if state == "unknown":
        return "applied", {**applied, "execution_state": "unknown"}, []
    changes = {**applied, "execution_state": state, "outcome": outcome,
               "last_trusted_state": state, "last_trusted_at": obs["observed_at"]}
    if state == "ended":
        changes["closed_at"] = received
    return "applied", changes, []


def _bind(tx: Tx, attempt: str, ref: dict) -> str:
    for key in ("kind", "executor_namespace", "opaque_id"):
        if not ref.get(key):
            raise RLError("INVALID", f"execution_ref needs {key}")
    if ref["kind"] not in ENUMS["exec_kind"]:
        raise RLError("INVALID", f"execution_ref.kind must be one of {', '.join(ENUMS['exec_kind'])}")
    found = [x for x in tx.all("ExecRef", opaque_id=str(ref["opaque_id"]))
             if x["executor_namespace"] == ref["executor_namespace"]]
    if found:
        xid = found[0]["id"]
        if found[0]["kind"] != ref["kind"]:
            raise RLError("CONFLICT", f"{xid} ({ref['executor_namespace']}/{ref['opaque_id']}) is a {found[0]['kind']}")
    else:
        xid = tx.next_id("ExecRef")
        tx.create("ExecRef", {"id": xid, "kind": ref["kind"], "executor_namespace": ref["executor_namespace"],
                              "opaque_id": str(ref["opaque_id"]), "locator": ref.get("locator")})
    bound = tx.out(attempt, "bound_to")
    if bound and bound[0]["id"] != xid:
        raise RLError("CONFLICT", f"{attempt} is bound to {bound[0]['id']}; a re-execution is a new attempt "
                                  "(rl run register --run <RUN> --request-id <new>)")
    if not bound:
        tx.link("bound_to", attempt, xid)
    return xid


def record(store: Store, obs: dict) -> dict:
    unknown = set(obs) - OBS_FIELDS
    if unknown:
        raise RLError("INVALID", f"unknown observation fields {sorted(unknown)}; allowed: {sorted(OBS_FIELDS)}")
    for key in ("attempt_id", "source", "observed_at", "execution_state"):
        if obs.get(key) in (None, ""):
            raise RLError("INVALID", f"observation needs {key}")
    obs = {**obs, "outcome": obs.get("outcome") or "unknown"}
    if obs["execution_state"] not in ENUMS["execution_state"] or obs["outcome"] not in ENUMS["outcome"]:
        raise RLError("INVALID", f"execution_state is one of {ENUMS['execution_state']}; outcome one of {ENUMS['outcome']}")
    if obs["outcome"] != "unknown" and obs["execution_state"] != "ended":
        raise RLError("INVALID", "an outcome other than unknown needs execution_state ended")
    if obs.get("correction_of") and not obs.get("note"):
        raise RLError("INVALID", "a correction needs a note stating the evidence for it")
    parse_time(obs["observed_at"])
    if obs.get("occurred_at"):
        parse_time(obs["occurred_at"])  # the source's own event time; stored as evidence, never used for ordering
    content = {k: v for k, v in obs.items() if k != "record_id"}
    digest = digest_json(content)
    obs["record_id"] = obs.get("record_id") or f"OBS-{short(digest, 16)}"
    warnings = []
    ref = obs.get("execution_ref")
    if ref and ref.get("kind") == "local_process" and str(ref.get("opaque_id", "")).isdigit() \
            and (ref.get("executor_namespace") or "").count(":") < 1:
        warnings.append("a bare PID is not unique: include host and boot/start identity in executor_namespace")
    with store.write() as tx:
        existing = tx.get(obs["record_id"], "Observation")
        if existing:
            if existing["content_digest"] != digest:
                raise RLError("CONFLICT", f"record {obs['record_id']} was already received with different content")
            return {"record_id": obs["record_id"], "duplicate": True,
                    "summary": f"duplicate {obs['record_id']} ignored (already {existing['effect']})"}
        att = tx.require(obs["attempt_id"], "Attempt")
        if obs.get("correction_of"):
            target = tx.require(obs["correction_of"], "Observation")
            if target["attempt_id"] != att["id"]:
                raise RLError("INVALID", f"{obs['correction_of']} belongs to {target['attempt_id']}")
        xid = _bind(tx, att["id"], ref) if ref else None
        received = now_utc()
        effect, changes, issues = _project(att, obs, received)
        if issues:
            changes["issues"] = (att.get("issues") or []) + issues
        tx.create("Observation", {"id": obs["record_id"], "attempt_id": att["id"], "source": obs["source"],
                                  "observed_at": obs["observed_at"], "received_at": received,
                                  "source_sequence": obs.get("source_sequence"),
                                  "execution_state": obs["execution_state"], "outcome": obs["outcome"],
                                  "raw_state": obs.get("raw_state"), "source_ref": obs.get("source_ref"),
                                  "occurred_at": obs.get("occurred_at"),
                                  "correction_of": obs.get("correction_of"), "note": obs.get("note"),
                                  "content_digest": digest, "effect": effect})
        if changes:
            att = tx.update(att["id"], changes)
        usage = None
        if obs.get("usage"):
            usage = _usage(tx, obs["usage"], [att["id"]])
        tx.event("run.record", att["id"], f"{obs['record_id']} {obs['execution_state']}/{obs['outcome']} -> {effect}")
    return {"record_id": obs["record_id"], "effect": effect, "attempt": brief(att), "execution_ref": xid,
            "usage": usage, "warnings": warnings,
            "summary": f"{obs['record_id']} {effect}; {brief(att)}" + (f"; {issues[0]}" if issues else "")}


# -- results --------------------------------------------------------------------------------------------------


def _verify_artifacts(project: Project, artifacts: list) -> tuple[list[dict], str]:
    out, counts = [], {}
    for art in artifacts:
        if not isinstance(art, dict) or not (art.get("path") or art.get("uri")):
            raise RLError("INVALID", "each artifact is {path: <local path>} or {uri: <remote>}, with optional digest")
        entry = {k: art.get(k) for k in ("path", "uri", "role", "digest") if art.get(k)}
        if art.get("path"):
            local = project.abs(art["path"])
            if not local.is_file():
                entry["check"] = "missing_locally"
            else:
                actual = digest_file(local)
                entry["local_digest"] = actual
                reported = art.get("digest")
                if not reported or not reported.startswith("blake2b:"):
                    entry["check"] = "read_locally" + ("" if not reported else "; reported digest uses another algorithm, not compared")
                else:
                    entry["check"] = "verified_locally" if reported == actual else "digest_mismatch"
        else:
            entry["check"] = "remote_reference_not_read"
        counts[entry["check"].split(";")[0]] = counts.get(entry["check"].split(";")[0], 0) + 1
        out.append(entry)
    scope = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no artifacts"
    return out, scope


def _mismatches(spec: dict, spec_digest: str, manifest: dict) -> tuple[list[dict], list[str]]:
    found, unreported = [], []
    if manifest.get("spec_digest") and manifest["spec_digest"] != spec_digest:
        found.append({"field": "spec_digest", "expected": spec_digest, "actual": manifest["spec_digest"]})
    pairs = [(k, spec.get(k), manifest.get(k)) for k in COMPARED]
    code = spec.get("code_ref") if isinstance(spec.get("code_ref"), dict) else {}
    actual_code = manifest.get("code_ref") if isinstance(manifest.get("code_ref"), dict) else {}
    pairs += [(f"code_ref.{k}", code.get(k), actual_code.get(k)) for k in ("commit", "content_digest")]
    for field, expected, actual in pairs:
        if expected in (None, "unknown"):
            continue
        if actual is None:
            unreported.append(field)
        elif actual != expected:
            found.append({"field": field, "expected": expected, "actual": actual})
    for field in ("arm", "seed", "replicate"):  # which run it is: compared when the manifest names it
        expected, actual = spec.get(field), manifest.get(field)
        if expected is not None and actual is not None and str(actual) != str(expected):
            found.append({"field": field, "expected": expected, "actual": actual})
    return found, unreported


def _best(results: list[dict]) -> str:
    statuses = {r["status"] for r in results if r["status"] != "rejected"}
    return "complete" if "complete" in statuses else "partial" if "partial" in statuses else (
        "rejected" if results else "absent")


def import_result(store: Store, attempt: str, manifest: dict, request_id: str) -> dict:
    if manifest.get("status") not in ("partial", "complete"):
        raise RLError("INVALID", "manifest.status is partial or complete")
    artifacts = manifest.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise RLError("INVALID", "manifest.artifacts is a list")
    verification, scope = _verify_artifacts(store.project, artifacts)
    manifest_digest = digest_json(manifest)
    payload = digest_json({"attempt": attempt, "manifest": manifest})
    with store.write() as tx:
        previous = tx.request("run.import-result", request_id, payload)
        if previous:
            return {**previous, "duplicate": True, "summary": f"duplicate request {request_id}: {previous['result']}"}
        att = tx.require(attempt, "Attempt")
        run = tx.require(att["run_id"], "Run")
        prior = tx.all("Result", attempt_id=attempt)
        same = [r for r in prior if r["manifest_digest"] == manifest_digest]
        if same:
            result = {"result": same[0]["id"], "attempt": attempt}
            tx.remember("run.import-result", request_id, payload, result)
            return {**result, "duplicate": True, "summary": f"same manifest already imported as {same[0]['id']}"}
        mismatches, unreported = _mismatches(run["spec"], run["spec_digest"], manifest)
        bad = [v["path"] for v in verification if v["check"] == "digest_mismatch"]
        rid = tx.next_id("Result")
        scope_text = scope + (f"; fields not reported: {', '.join(unreported)}" if unreported else "")
        tx.create("Result", {"id": rid, "attempt_id": attempt, "request_id": request_id, "revision": len(prior) + 1,
                             "status": manifest["status"], "manifest": manifest, "manifest_digest": manifest_digest,
                             "spec_match": not mismatches, "mismatches": mismatches or None,
                             "verification": verification, "verification_scope": scope_text,
                             "usable_for_planned_comparison": not mismatches and not bad})
        tx.update(attempt, {"result_status": _best(prior + [{"status": manifest["status"]}])})
        result = {"result": rid, "attempt": attempt}
        tx.remember("run.import-result", request_id, payload, result)
        tx.event("run.import_result", attempt, f"{rid} {manifest['status']} ({scope_text})")
    notes = []
    if mismatches:
        notes.append("differs from RunSpec in " + ", ".join(m["field"] for m in mismatches)
                     + ": kept, but not usable as the pre-specified comparison (a manifest copies RunSpec fields "
                       "exactly or leaves them out)")
    if bad:
        notes.append(f"digest mismatch: {', '.join(bad)}")
    if att["execution_state"] != "ended":
        notes.append(f"attempt is still {att['execution_state']}: a result file is not an execution end record")
    return {**result, "mismatches": mismatches, "verification_scope": scope_text,
            "summary": f"{rid} {manifest['status']} for {attempt} (checked: {scope_text})"
                       + ("; " + "; ".join(notes) if notes else "")}


def reject_result(store: Store, result_id: str, reason: str) -> dict:
    """What rests on the result (the evidence drawn from it, their hypotheses, decisions based on either) is
    marked for re-checking; whether it still holds is the researcher's judgment."""
    with store.write() as tx:
        res = tx.require(result_id, "Result")
        tx.update(result_id, {"status": "rejected", "rejected_reason": reason, "usable_for_planned_comparison": False})
        results = tx.all("Result", attempt_id=res["attempt_id"])
        tx.update(res["attempt_id"], {"result_status": _best(results)})
        flagged = recheck_dependents(tx, result_id, f"{result_id} rejected: {reason}")
        tx.event("run.reject_result", result_id, reason)
    return {"result": result_id, "flagged": flagged,
            "summary": f"{result_id} rejected: {reason}" + (f"; re-check {', '.join(flagged)}" if flagged else "")}


# -- usage ----------------------------------------------------------------------------------------------------


def _usage(tx: Tx, usage: dict, attempts: list[str], revise: bool = False) -> dict:
    for key in ("accounting_ref", "quantity", "unit", "resource_type"):
        if usage.get(key) in (None, ""):
            raise RLError("INVALID", f"usage needs {key} (unreported usage is simply not recorded, never zero)")
    if ":" not in usage["accounting_ref"]:
        raise RLError("INVALID", "accounting_ref needs a namespace, e.g. miyabi-pbs:3731052.opbs or local:<host>:<run>")
    body = {k: usage.get(k) for k in ("quantity", "unit", "resource_type", "hardware", "basis", "source_ref")}
    body["basis"] = body["basis"] or "reported"
    digest = digest_json({**body, "attempts": sorted(attempts)})
    current = [u for u in tx.all("Usage", accounting_ref=usage["accounting_ref"]) if not u.get("superseded")]
    if current:
        old = current[-1]
        if old["content_digest"] == digest:
            return {"usage": old["id"], "duplicate": True}
        if not revise:
            raise RLError("CONFLICT", f"{usage['accounting_ref']} is already recorded as {old['id']} "
                                      f"({old['quantity']:g} {old['unit']}, {old.get('source_ref') or 'no source_ref'}); "
                                      "if that record is right there is nothing to add, otherwise pass --revise "
                                      "to record a corrected revision")
    uid = tx.next_id("Usage")
    tx.create("Usage", {"id": uid, "accounting_ref": usage["accounting_ref"], "quantity": float(body["quantity"]),
                        "unit": body["unit"], "resource_type": body["resource_type"], "hardware": body["hardware"],
                        "basis": body["basis"], "source_ref": body["source_ref"], "note": usage.get("note"),
                        "revision": len(current) + 1 if current else 1, "content_digest": digest})
    for old in current:
        tx.update(old["id"], {"superseded": True})
        tx.link("supersedes", uid, old["id"])
    for a in attempts:
        tx.require(a, "Attempt")
        tx.link("covers", uid, a)
    return {"usage": uid, "duplicate": False}


def add_usage(store: Store, usage: dict, attempts: list[str], revise: bool = False) -> dict:
    with store.write() as tx:
        out = _usage(tx, usage, attempts, revise)
        if not out["duplicate"]:
            tx.event("run.usage", out["usage"], f"{usage['quantity']} {usage['unit']} {usage['accounting_ref']}")
    return {**out, "summary": f"{out['usage']} {'already recorded' if out['duplicate'] else 'recorded'} "
                              f"({usage['quantity']} {usage['unit']}, {usage['accounting_ref']})"}


def usage_summary(tx: Tx) -> dict:
    totals: dict[tuple, float] = {}
    covered = set()
    for u in tx.all("Usage"):
        if u.get("superseded"):
            continue
        key = (u["resource_type"], u["unit"], u.get("hardware") or "-", u["basis"])
        totals[key] = totals.get(key, 0.0) + u["quantity"]
        covered |= {e["id"] for e in tx.out(u["id"], "covers")}
    attempts = [a["id"] for a in tx.all("Attempt") if a["execution_state"] != "prepared"]
    return {"totals": [{"resource_type": k[0], "unit": k[1], "hardware": k[2], "basis": k[3], "quantity": v}
                       for k, v in sorted(totals.items())],
            "unreported_attempts": [a for a in attempts if a not in covered]}


# -- reading --------------------------------------------------------------------------------------------------


def _age_h(stamp: str | None) -> float | None:
    if not stamp:
        return None
    return (datetime.now(timezone.utc) - parse_time(stamp)).total_seconds() / 3600


def describe_attempt(tx: Tx, att: dict) -> str:
    refs = [tx.require(e["id"]) for e in tx.out(att["id"], "bound_to")]
    ref_text = ", ".join(f"{r['kind']} {r['executor_namespace']}/{r['opaque_id']}" for r in refs) or "no external ref yet"
    age = _age_h(att.get("last_observed_at"))
    fresh = "never observed" if age is None else f"last observed {age:.1f}h ago" + (
        " (stale? refresh with your execution tool)" if age > STALE_AFTER_H and att["execution_state"] != "ended" else "")
    trusted = (f"; last trusted {att['last_trusted_state']} at {att['last_trusted_at']}"
               if att.get("last_trusted_state") and att["execution_state"] == "unknown" else "")
    review = f"; code review: {code_review_note(att['code_review'], tx)}" if att.get("code_review") else ""
    return f"{brief(att)} {ref_text}; {fresh}{trusted}{review}"


def show(store: Store, entity_id: str | None = None, active: bool = False) -> dict:
    with store.read() as tx:
        if entity_id:
            rec = tx.require(entity_id, "Run", "Attempt", "Experiment")
            runs = ([rec] if rec["_label"] == "Run" else
                    [tx.require(rec["run_id"])] if rec["_label"] == "Attempt" else tx.all("Run", experiment_id=entity_id))
            lines = []
            for run in runs:
                lines.append(f"{run['id']} {run['experiment_ref']} spec {short(run['spec_digest'])} "
                             f"request {run['request_id']}")
                lines.append(f"  spec: {run['spec']}")
                for att in tx.all("Attempt", run_id=run["id"]):
                    if rec["_label"] == "Attempt" and att["id"] != entity_id:
                        continue
                    lines.append(f"  {describe_attempt(tx, att)}")
                    for issue in att.get("issues") or []:
                        lines.append(f"    issue: {issue}")
                    for o in sorted(tx.all("Observation", attempt_id=att["id"]), key=lambda o: o["received_at"])[-5:]:
                        lines.append(f"    obs {o['id']} {o['observed_at']} {o['source']}: "
                                     f"{o['execution_state']}/{o['outcome']} -> {o['effect']}"
                                     + (f" (event at {o['occurred_at']})" if o.get("occurred_at") else ""))
                    for r in tx.all("Result", attempt_id=att["id"]):
                        lines.append(f"    result {r['id']} r{r['revision']} {r['status']} spec_match={r['spec_match']} "
                                     f"({r['verification_scope']}) metrics={r['manifest'].get('metrics')}")
            return {"summary": "\n".join(lines) or f"no runs for {entity_id}"}
        atts = tx.all("Attempt")
        if active:
            atts = [a for a in atts if a["execution_state"] != "ended" or a.get("needs_check")]
        lines = [describe_attempt(tx, a) for a in atts]
        usage = usage_summary(tx)
    head = f"{len(lines)} {'unfinished ' if active else ''}attempt(s); recorded facts only — nothing was queried"
    lines += [f"usage: {t['quantity']:g} {t['unit']} {t['resource_type']} on {t['hardware']} ({t['basis']})"
              for t in usage["totals"]]
    if usage["unreported_attempts"]:
        lines.append(f"usage unreported for: {', '.join(usage['unreported_attempts'])}")
    return {"summary": "\n".join([head, *lines]), "usage": usage}


def digest_paths(paths: list[str]) -> dict:
    """blake2b digests for files, and a combined digest for directories (sorted relative paths + file digests)."""
    out = {}
    for p in paths:
        path = Path(p)
        if path.is_dir():
            files = sorted(f for f in path.rglob("*") if f.is_file() and "__pycache__" not in f.parts
                           and ".git" not in f.parts)
            out[p] = digest_json([[str(f.relative_to(path)), digest_file(f)] for f in files])
        elif path.is_file():
            out[p] = digest_file(path)
        else:
            raise RLError("NOT_FOUND", f"{p} does not exist")
    return {"digests": out, "summary": "\n".join(f"{v}  {k}" for k, v in out.items())}
