# Codex notes

- `rl` is this plugin's `bin/rl`; it is not on PATH. The session-start note prints its absolute path, and the
  sessions the watcher opens get it in `SMOOTH_RESEARCH_RL` and in their pack: write that path in each command.
- Hooks: Codex asks once to trust the plugin's hooks. Without them there is no session-start note.
- A session the watcher opens gets `-m <model>`, `-c model_reasoning_effort=..` for an `@effort`, `--search`
  when it is an advisor, and `agents.args.codex` from `smooth-research.yaml`; rl adds no sandbox or approval
  flags (set them there if you want them).
- Codex asks whether to trust the folder: the watcher answers that question for the folder it opened the session
  in. Other dialogs (hooks to review, an approval) are recorded for the coordinator (`rl agent dialog`, `rl
  agent answer`); trusting hooks is the user's decision.
- Subagents run on a smaller model, or the same model at a lower effort.
