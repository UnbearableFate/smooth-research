# Codex notes

- `rl` is this plugin's `bin/rl`; it is not on PATH. The session-start note prints its absolute path, and the
  sessions rl opens get it in `SMOOTH_RESEARCH_RL` and in their pack: write that path in each command.
- Hooks: SessionStart, Stop and UserPromptSubmit. Codex asks to review them ("Hooks need review") after every
  plugin update: the user trusts them once. Without them there is no session-start note, and a session that was
  busy when a command told it gets the new items only from the observer or `rl inbox`.
- A session rl opens gets `-m <model>`, `-c model_reasoning_effort=..` for an `@effort`, `--search` when it is an
  advisor, and `agents.args.codex` from `smooth-research.yaml` (`codex resume <id>` when it goes on with its earlier
  conversation); rl adds no sandbox or approval flags (set them there if you want them). Commands that hand work on
  call herdr from inside the session: the sandbox set there must allow it, or they print "could not notify".
- Codex asks whether to trust the folder: rl answers that question for the folder it opened the session in. Other
  dialogs (hooks to review, an approval) are recorded for the coordinator (`rl agent dialog`, `rl agent answer`);
  trusting hooks is the user's decision.
- Subagents run on a smaller model, or the same model at a lower effort. The coordinator's tree subagent is the
  exception: the `models.tree` entry for codex (default `gpt-6.2-astra@xhigh`). If the coordinator cannot open a
  subagent, it follows `rl tree prompt` itself.
