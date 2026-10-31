---
name: research-review
description: Independent review by the configured reviewer in a Smooth Research project, of code or of a key conclusion. Code - before a costly run on a new core operator, training or evaluation path, loss mask, data split, adapter injection, checkpoint I/O or distributed change, for a final candidate snapshot, or after a merge that touches these; not for new seeds of an already reviewed spec or unrelated edits. Conclusion - before recording a hypothesis as supported or challenged, stopping a route, or recommending a formal experiment.
---

# Research review

The reviewer configured in `smooth-research.yaml` (models.reviewer, the first available entry) checks one
exact scope in its own session, read-only. Two subjects:
the code an implementer wrote (requested by whoever wrote it, usually a researcher) and a key conclusion
the coordinator is about to record (requested by the coordinator). The CLI is the one described in the
smooth-research skill: {{rl_short}}

## Rounds and severity

- Every finding has a severity: high (the result would be wrong or the experiment invalid), medium (likely to
  distort the result or waste much of the budget), low (worth fixing; does not change the conclusion). A
  review passes when its verdict is pass, or issues with only low findings open.
- A round holds at most two reviews: the first, and one re-check (`--recheck-of RV-x`). When medium or higher
  findings remain after the second, the CLI hands the round to the coordinator (a NOTE and an attention
  item), who approves it with a reason (`{{rl}} review approve RV-x --rationale ..`) or returns the work.
  Approving is the coordinator's decision; a session that `{{rl}} agent open` started cannot approve.
- A third request in the same round stops with LIMIT. A further review starts a new round, with the reason:
  `--recheck-of RV-x --new-round "<why the code needs another review>"`.
- In herdr the reviewer is a session that the coordinator assigns (`{{rl}} agent open --role reviewer
  --ticket RV-x`). It answers with `{{rl}} review answer RV-x --answer <file>` and talks with the requester
  through `{{rl}} agent tell`; the re-check goes to the same reviewer session. Without herdr, `{{rl}} review
  run` asks the configured reviewers headless. Rounds count the same way.

## Code

1. Request with a neutral question and the exact scope:
   `{{rl}} review request --target E-x --files src/model.py,src/train.py --configs configs/run.yaml
   --ignore-keys seed,output_dir --author-model <model that wrote the code> --question "<what could make
   the result wrong?>" --scope "<new path under review, invariants that must hold>"`
   - `--author-model` is the implementer (you, or the researcher that wrote it), not the coordinator.
   - If the scope fingerprint (code, config minus the ignored keys, dependencies, scope text) matches an
     earlier answered review, it is reused automatically and nothing is sent.
2. Run it: in herdr the request reaches the coordinator, who assigns a reviewer session; otherwise
   `{{rl}} review run RV-x --background`, then later `{{rl}} review show RV-x`. The configured reviewers are
   tried in order; failures fall through to the next one and are recorded. Keep doing independent cheap work
   meanwhile; do not wait idle.
3. Read the answer in `reports/reviews/RV-x.md` (default reports directory): verdict pass | issues |
   blocking, findings with severity, location, evidence, impact, fix, verification, plus what was and was
   not checked.
4. Fix real problems, then re-check only the affected scope, once per round:
   `{{rl}} review request ... --recheck-of RV-x`. For a finding you dispute, keep its text, record your
   reasoning (`{{rl}} decide --targets E-x --action modify|continue --rationale .. --based-on RV-x`) and
   leave the decision to the coordinator if it stays open; never re-roll reviewers until something says pass.
5. If no configured reviewer can be called, the ticket says `unavailable`. That is not a pass: low-cost
   independent work may continue, but scaled-up runs that depend on the reviewed path wait (a researcher
   blocks its task with `--reason model_unavailable`).
6. `{{rl}} run register` checks each run against the code reviews of its experiment (and of the experiment
   its code comes from; name others with `--review RV-x`): every reviewed path must still match, in the code
   the run executes, a version that a passing review saw (configs without the ignored keys).
   The result (covered, stale with the changed paths, not reviewed, or `--no-review` with its reason) is
   recorded on the attempt and shown in conclusion review packages and `{{rl}} report`, with the reviews the
   coordinator approved after the registration. A review requested
   before scopes were recorded cannot be checked: request it again with the same scope (the answer is reused).

## Conclusion

1. Before `{{rl}} decide` sets a hypothesis supported or challenged, stops a route, or records a stage
   delivery with recommend_formal or stop:
   `{{rl}} review conclusion --target H-x --proposal "<the conclusion you plan to record, with its evidence
   level and scope>" --author-model <your model id>`. Add the model of a researcher whose interpretation you
   adopt. The package is built from the records: the hypothesis, each linked evidence with its attempts
   and results, the contracts and per-arm numbers, code reviews, earlier decisions and the competing open
   hypotheses. Unchanged records and wording reuse an earlier answer.
2. `{{rl}} review run RV-x` (or `--background`), then read `reports/reviews/RV-x.md` (default reports
   directory): verdict and findings (overclaim, wrong_level, out_of_scope, unruled_alternative, data_mismatch).
3. Act on it: weaken the wording, run the missing control, or keep the conclusion with a recorded reason.
   Then `{{rl}} decide ... --based-on EV-x,RV-x`. A finding you dispute stays in the record; ask for one
   re-check (`--recheck-of RV-x`) if it matters. When medium or higher findings remain after the re-check,
   approve the round with your reason or change the conclusion.
4. A key decision without an answered conclusion review stays in `{{rl}} status` attention until one is
   linked: `{{rl}} link D-x based_on RV-x`. An unavailable or blocking review is not a pass.

## Notes

- Deterministic checks (recomputing numbers, manifest checks) belong in scripts, not in model reviews.
- The relation to the author's model family is recorded on each answer (cross_model,
  cross_model_unverified, same_family_independent_session, same_family_unverified); it is not a gate. For a
  review from another family, list such a model first under models.reviewer.
- An answer obtained elsewhere (another session, a product UI) is imported honestly:
  `{{rl}} review import RV-x --answer <file> --source "<who/where>" --model-observed <model>`.
- Recorded per ticket: subject (code or conclusion), requested entry, observed model (or unverified),
  session id, author models, relation, cost when reported.
