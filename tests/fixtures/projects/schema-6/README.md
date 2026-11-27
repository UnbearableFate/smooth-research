# Frozen project, record format 6

An `rl export` snapshot made on 2026-11-27 by the CLI that writes record format 6 (the 2026-11-27 redesign: the
plugin guides and records; process rules are flags on the records; the tree is recorded from an approved draft): one
hypothesis taken from the tree draft (`rl tree record`, with its public prep task T-0001 and the approval RQ-0001 on
the route decision D-0001) to its final verdict (tests/flow.py, `concluded`), then a question of the coordinator to
the scientist, answered, and the prep task released by the scientist (a `role` flag on T-0001).

Never regenerate or edit it. tests/test_upgrade.py restores it and runs the commands on it, so a later plugin
must keep reading and upgrading it. When the record format changes, add a migration and a new frozen project
next to this one (`schema-<N>/`), and keep this one and the earlier ones.
