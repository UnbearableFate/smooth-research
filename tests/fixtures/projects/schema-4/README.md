# Frozen project, record format 4

An `rl export` snapshot made on 2026-11-05 by the CLI that writes record format 4 (reviews and tasks bound to the
revisions they were of): the schema-3 project next to this one, restored and so upgraded (its conclusion review
RV-0002 got the revision of H1 it reviewed from the migration), then a task on EH1/2 that sealed its contract
(T-0002, bound to EH1/2@1), the contract revised to EH1/2@2, a code review of EH1/2 bound to @2 (RV-0003) and a
conclusion review of H1 bound to H1@2 and the evidence it saw (RV-0004).

Never regenerate or edit it. tests/test_upgrade.py restores it and runs the commands on it, so a later plugin
must keep reading and upgrading it. When the record format changes, add a migration and a new frozen project
next to this one (`schema-<N>/`), and keep this one.
