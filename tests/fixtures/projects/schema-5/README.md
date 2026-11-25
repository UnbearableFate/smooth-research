# Frozen project, record format 5

An `rl export` snapshot made on 2026-11-25 by the CLI that writes record format 5 (the 2026-11 redesign: tasks,
phases, asks, sessions, messages): one hypothesis taken from the tree task to its final verdict (tests/flow.py,
`concluded`), then a question of the coordinator to the scientist, answered.

Never regenerate or edit it. tests/test_upgrade.py restores it and runs the commands on it, so a later plugin
must keep reading and upgrading it. When the record format changes, add a migration and a new frozen project
next to this one (`schema-<N>/`), and keep this one.
