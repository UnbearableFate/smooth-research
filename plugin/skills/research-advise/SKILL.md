---
name: research-advise
description: Consult the configured advisors at a research bottleneck in a Smooth Research project - when valid tests cannot separate the key explanations, several distinct changes gave no new information, a mechanism works in isolation but fails in the model, new literature changes the value of a route, or before a large cost step. Not for a known code bug. Also for an advisor session (advisor-AD-x).
---

# Research advice

The CLI is the one described in the smooth-research skill: {{rl_short}}

- A scientist at a bottleneck asks: `{{rl}} advise ask --question "<what you cannot decide, what you tried>"`
  and ends its turn. The watcher opens an advisor session (`advisor-AD-x`, the first available entry of
  `models.advisor`) that sees the hypothesis, its evidence and decisions through its pack. The answer comes back
  as an item; the scientist weighs it and decides. Advice is not a decision and is never cited as evidence.
- A review round that ran out of passes may go to an advisor for a ruling on its blocking findings.
- An advisor session runs `{{rl}} pack`, writes its answer to a file and records it: `{{rl}} advise answer AD-x
  --doc <file>` (a ruling also gives `--rulings '[{"n": 1, "ruling": "uphold|withdraw", "reason": ".."}]'`).
  It assesses the state and the evidence, names the bottleneck, proposes at most three mechanism-distinct
  routes and may challenge the scientist's reading.
