# Frozen project, record format 3

An `rl export` snapshot made on 2026-11-05 by the CLI that writes record format 3 (hierarchical labels on
hypotheses and experiments): the schema-2 project next to this one, restored and so upgraded (its records got
their labels from the migration), then a hypothesis placed with `--parent` (H1.2), one that opens a new line
with `--root` (H2) and an experiment anchored with `--anchor` (EH1/2), whose labels were given at creation.

Never regenerate or edit it. tests/test_upgrade.py restores it and runs the commands on it, so a later plugin
must keep reading and upgrading it. When the record format changes, add a migration and a new frozen project
next to this one (`schema-<N>/`), and keep this one.
