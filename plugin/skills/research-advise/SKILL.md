---
name: research-advise
description: Consult the strongest configured model at a research bottleneck in a Smooth Research project - when valid tests cannot separate the key explanations, several distinct changes gave no new information, a mechanism works in isolation but fails in the model, new literature changes the value of a route, before a large cost step, or when the user asks for advice. Not for a known code bug.
---

# Research advice

The advisor sees a compact evidence package, may challenge your interpretation, and proposes a few
mechanism-distinct routes. The CLI is the one described in the smooth-research skill: {{rl_short}}

1. State the bottleneck precisely, then prepare the package:
   `{{rl}} advise prepare --target H-x --question "<the decision you cannot make and why>"`.
   It gathers the goal, key positive and negative evidence, routes already stopped, surviving explanations,
   constraints, nearest literature and an artifact index into `research/packages/AD-x.request.md` (default
   records directory).
   The same question with the same evidence returns the existing ticket: consult again only with new data.
2. Ask: `{{rl}} advise run AD-x --background`; check with `{{rl}} advise show AD-x`. The configured primary
   advisor is tried first, then the fallbacks. If the strongest mode exists only in a product UI (a
   `manual:` entry), give the user the package path and import their answer verbatim:
   `{{rl}} advise import AD-x --answer <file> --source "<product and mode>" --model-observed <model>`.
3. Read `reports/advice/AD-x.md` (default reports directory). Each proposal should state what it explains,
   how it differs from tried routes, a distinguishing prediction with controls, the cheapest test with its
   cost, and what would stop it.
4. Turn only the proposals you select into new hypotheses and one cheap discriminating experiment:
   `{{rl}} hypothesis add --title .. --statement .. --derived-from AD-x --origin "AD-x proposal 2"`.
   Advice is a source, never evidence. Anything beyond the agreed scope or budget goes to the user.
5. If nothing is worth its cost, pausing is a valid outcome: `{{rl}} decide --targets H-x --action pause
   --rationale .. --based-on AD-x --reopen-condition ..`.

While a consultation is pending, continue other independent work. Do not loop on consultations, and do
not count one known code bug as a scientific bottleneck.
