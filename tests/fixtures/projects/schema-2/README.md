# Frozen project, record format 2

An `rl export` snapshot of a small project with every record type (hypotheses and a revision, a sealed
contract, a task delivered and accepted, a coordinator session, messages, a run with observations, a result
and usage, evidence, code and conclusion reviews, an advice round, a paper and its link, route and stage
decisions), made on 2026-10-31 by the CLI that writes record format 2.

Never regenerate or edit it. tests/test_upgrade.py restores it and runs the commands on it, so a later plugin
must keep reading and upgrading it. When the record format changes, add a migration and a new frozen project
next to this one (`schema-<N>/`), and keep this one.
