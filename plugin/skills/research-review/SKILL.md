---
name: research-review
description: Independent review in a Smooth Research project - of an experiment's or preparation task's code before it is handed on or run, or of a draft verdict or stop before it becomes final. Use when a scientist or researcher needs a review round, or when you are a reviewer session (reviewer-RV-x).
---

# Research review

A review round is one author, one target and one reviewer session (`reviewer-RV-x`, on the first available entry
of `models.reviewer`), opened by the request. The CLI is the one described in the smooth-research skill:
{{rl_short}}

## Asking for one

- Code (an execute task's design before dispatch, or a preparation task's code): `{{rl}} review request T-x`,
  after the code is committed and usually after `{{rl}} task test T-x` passes. The review is of the committed head.
- Conclusion (a draft verdict or stop): `{{rl}} review request D-x`, without `--question`: the reviewer judges from
  the records, not from your account. Then `{{rl}} decide --final D-x`.
- Recheck after fixes: `{{rl}} review request T-x --recheck-of RV-x` (for a conclusion, the revised draft:
  `{{rl}} review request D-y --recheck-of RV-x`). The same command moves a review its reviewer has not answered yet
  to your newer commit or draft; that is not another review.

Then go on with other work or end your turn: the answer comes back to you as an item.

## Rules of a round

- At most two reviews: review, fix, recheck, fix. A first review that passes with nothing open ends the round.
  The recheck ends it whatever remains; the author makes the last fixes and goes on, and findings still open stay
  on the record as flags. More review is a new round.
- Both reviews are by the same reviewer session: the recheck wakes it, and it remembers what it found.
- Findings are method (the design: what is tested and how it is measured) or engineering (it does not run as
  designed), with severity low, medium or high. A finding in the protected scope is method unless the reviewer
  says why not. Nothing is refused for an open finding: dispatching with a method finding open, or running
  evidence with a method or a medium or high finding open, is flagged, and so is going on without a review.
- The author may reject a finding with a reason: `{{rl}} review reject RV-x#Fn --reason ..`. At the recheck the
  reviewer withdraws it (closes it) or keeps it, which marks it disputed.
- The reviewer answers with the target it reviewed: `{{rl}} review answer RV-x --verdict pass|fail --at
  <commit|D-x> ...`, exactly as its card prints it (`<commit>+E-x@n#..` when the design changed at the same commit
  while the review waited). An answer on anything but the round's current target is refused, so no verdict lands
  on code, a design or a draft its reviewer did not see.
- The reviewer and the owner of a finding discuss on the round: `{{rl}} discuss RV-x#Fn --text ..`; each entry
  is answered once (`{{rl}} reply MSG-x --text ..`).
- A reviewer session runs `{{rl}} pack` and follows its role card; it never edits code.
- Advisors take part only when someone asks them (`{{rl}} advise ask`).
