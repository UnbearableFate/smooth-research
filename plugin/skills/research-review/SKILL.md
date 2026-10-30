---
name: research-review
description: Cross-model code review for experiments in a Smooth Research project. Use before a costly run on a new core operator, training or evaluation path, loss mask, data split, adapter injection, checkpoint I/O or distributed change, for a final candidate snapshot, or after a merge that touches these. Not for new seeds of an already reviewed spec or unrelated edits.
---

# Research review

A reviewer from a different model family than the code's author checks one exact scope. The CLI is the
one described in the smooth-research skill: {{rl_short}}

1. Request with a neutral question and the exact scope:
   `{{rl}} review request --target E-x --files src/model.py,src/train.py --configs configs/run.yaml
   --ignore-keys seed,output_dir --author-model <model that wrote the code> --question "<what could make
   the result wrong?>" --scope "<new path under review, invariants that must hold>"`
   - `--author-model` is the implementer (you, or the researcher that wrote it), not the coordinator.
   - If the scope fingerprint (code, config minus the ignored keys, dependencies, scope text) matches an
     earlier cross-model review, it is reused automatically and nothing is sent.
2. Run it: `{{rl}} review run RV-x --background`, then later `{{rl}} review show RV-x`. The first configured
   candidate from another family is used; failures fall through to the next one and are recorded.
   Keep doing independent cheap work meanwhile; do not wait idle.
3. Read the answer in `reports/reviews/RV-x.md` (verdict pass | issues | blocking, findings with location,
   evidence, impact, fix, verification, plus what was and was not checked).
4. Fix real problems, then re-check only the affected scope:
   `{{rl}} review request ... --recheck-of RV-x`. For a finding you dispute, keep its text, record your
   reasoning (`{{rl}} decide --targets E-x --action modify|continue --rationale .. --based-on RV-x`) and
   ask for one independent re-check if it matters; never re-roll reviewers until something says pass.
5. If no eligible reviewer can be called, the ticket says `unavailable`. That is not a pass: low-cost
   independent work may continue, but scaled-up runs that depend on the reviewed path wait.

Notes
- Deterministic checks (recomputing numbers, manifest checks) belong in scripts, not in model reviews.
- A review by the same model family in another session (for example your own subagent) is an independent
  session review, not cross-model. Import such an answer honestly:
  `{{rl}} review import RV-x --answer <file> --source "<who/where>" --model-observed <model>`.
- Recorded per ticket: requested entry, observed model (or unverified), session id, author models,
  relation (cross_model, cross_model_unverified, same_family_independent_session), cost when reported.
