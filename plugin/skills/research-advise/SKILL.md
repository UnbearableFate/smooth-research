---
name: research-advise
description: Consult the configured advisors at a research bottleneck in a Smooth Research project - when valid tests cannot separate the key explanations, several distinct changes gave no new information, a mechanism works in isolation but fails in the model, new literature changes the value of a route, before a large cost step, or when the user asks for advice. Not for a known code bug.
---

# Research advice

Every advisor configured in `smooth-research.yaml` (models.advisor) sees the same compact evidence package
and answers on its own: it assesses the state and the evidence, explores the related research, names the
bottleneck, proposes at most three mechanism-distinct routes and recommends continuing, pausing or stopping.
It may challenge your interpretation. You weigh the answers together. The CLI is the one described in the
smooth-research skill: {{rl_short}}

1. State the bottleneck precisely, then prepare one advice round:
   `{{rl}} advise prepare --target H-x --question "<the decision you cannot make and why>"`.
   It gathers the goal, key positive and negative evidence, routes already stopped, surviving explanations,
   constraints, nearest literature and an artifact index into one package
   (`research/packages/advice-round-N.request.md`) and opens a ticket per
   advisor (AD-x, AD-y, ...).
   The same question on an unchanged package (no new evidence, literature or constraints) returns the
   existing tickets: consult again only with new data.
2. A project has `advice.max_rounds` rounds (default 3); a round that no advisor answered does not count.
   When they are used up, prepare stops with LIMIT: tell the user why another round is worth its cost, and
   only if they agree run it again with `--user-approved "<what the user said>"`.
3. Ask them all at once. In herdr, open one advisor session per ticket: `{{rl}} agent open --role advisor
   --ticket AD-x` (the ticket names its model). Each answers with `{{rl}} advise answer`, tells you its
   recommendation in one line and answers your follow-up questions through `{{rl}} agent tell`; a revised
   answer keeps the earlier one as a revision. Without herdr: `{{rl}} advise run AD-x AD-y --background`;
   check with `{{rl}} advise show AD-x`. If an advisor is a `manual:` entry (the mode exists only in a
   product UI), give the user the package path and import their answer verbatim:
   `{{rl}} advise import AD-x --answer <file> --source "<product and mode>" --model-observed <model>`.
4. Read every answer in `research/advice/`: the assessment, the related work
   with locators (and whether the advisor checked each claim), the bottleneck, the proposals and the
   recommendation. Each proposal should state what it explains, how it differs from tried routes, a
   distinguishing prediction with controls, the cheapest test with its cost, and what would stop it. Note
   where the advisors agree and where they differ.
5. Turn only the proposals you select into new hypotheses and one cheap discriminating experiment:
   `{{rl}} hypothesis add --title .. --statement .. --derived-from AD-x --origin "AD-x proposal 2"`.
   Advice is a source, never evidence. Anything beyond the agreed scope or budget goes to the user.
6. If nothing is worth its cost, pausing is a valid outcome: `{{rl}} decide --targets H-x --action pause
   --rationale .. --based-on AD-x,AD-y --reopen-condition ..`.
7. Advisors are costly: when you have an advisor's final advice, tell it so and close its session
   (`{{rl}} agent close <name>`).

While a consultation is pending, continue other independent work. Do not loop on consultations, and do
not count one known code bug as a scientific bottleneck.
