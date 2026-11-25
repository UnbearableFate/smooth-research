---
name: research-review
description: Independent review in a Smooth Research project - of an experiment's code and design before its runs, of a draft verdict or stop before it becomes final, or of a hypothesis tree before delivery. Use when a scientist or researcher needs a review round, or when you are a reviewer session (reviewer-RV-x).
---

# Research review

A review is a round on one record, answered by a reviewer session the watcher opens (`reviewer-RV-x`, on the
first available entry of `models.reviewer`). The CLI is the one described in the smooth-research skill:
{{rl_short}}

## Asking for one

- Design (before dispatching an execute task, or for code a researcher changed): `{{rl}} review request T-x`,
  after the code is committed and `{{rl}} task test T-x` passes. A recheck after fixes: `--recheck-of RV-x`.
- Conclusion (a draft verdict or stop): `{{rl}} review request D-x --kind conclusion`; then `{{rl}} decide --final
  D-x`. A draft reviewed for one hypothesis statement is not finalized after the statement changes.
- Tree: `{{rl}} review request T-x --kind tree` for a tree task.

Then end your turn: the review is a record that waits on the reviewer, and its answer comes back to you as an item.

## Rules of a round

- Findings are method (the design: what is tested and how it is measured) or engineering (it does not run as
  designed), with severity low, medium or high. A finding in the protected scope is method unless the reviewer
  says why not. Method findings block a dispatch; for evidence runs medium and high engineering findings block
  too. A known open finding blocks every new evidence attempt, also on the fast lane.
- Each segment (the scientist's, then the researcher's) has at most three passes; then the round escalates to
  the scientist who made the task, or to an advisor's ruling.
- The reviewer and the owner of a finding discuss on the round: `{{rl}} discuss RV-x#Fn --text ..`; each entry
  is answered once (`{{rl}} reply MSG-x --text ..`).
- A reviewer session runs `{{rl}} pack` and follows its role card; it never edits code.
