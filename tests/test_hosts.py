"""The options rl gives the installed claude, codex, pi and herdr are still in their help.

The other tests run fake hosts, so a release that renames or drops an option would show only in real use. This
test builds the command lines with rl's own code, reads each installed host's help (it starts no agent) and is
skipped for a host that is not installed. What the help does not list is not checked: codex -c config keys, tool
and key names, herdr's JSON fields and error codes.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from smooth_research import adapters, launch
from smooth_research.adapters import Entry

# The herdr commands rl runs (launch.py, watch.py, herdr.py) and what it gives them besides ids and text.
HERDR = {"agent": {"get": [], "read": ["--source", "visible"], "send-keys": [],
                   "prompt": ["--wait", "--until", "--timeout"],
                   "start": ["--kind", "--pane", "--timeout", "--"]},
         "tab": {"create": ["--cwd", "--label", "--env", "--no-focus"], "get": [], "close": []},
         "pane": {"get": [], "run": []}}
VALUED = {"--output-format", "--permission-mode", "--sandbox", "--mode"}  # their values are listed in the help too


def _help(*command: str) -> str:
    out = subprocess.run(list(command), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    return out.stdout + out.stderr


def _named(word: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text) is not None


def _host_checks(store, monkeypatch, host: str) -> list[tuple[tuple, str]]:
    """(help command, option or value) for every way rl runs the host: headless calls and herdr sessions."""
    made = []
    monkeypatch.setattr(adapters, "_run", lambda cmd, prompt, cwd, timeout:
                        made.append(cmd) or subprocess.CompletedProcess(cmd, 1, "", ""))
    entry = Entry(host, "some-model", "high")
    call = {"claude": adapters.call_claude, "codex": adapters.call_codex, "pi": adapters.call_pi}[host]
    for read_only, web in ((True, True), (True, False), (False, False)):
        call(entry, "the prompt", Path.cwd(), read_only, 1, add_dirs=[str(Path.cwd())], web=web)
    headless = ("codex", "exec", "--help") if host == "codex" else (host, "--help")
    lines = [(headless, cmd[len(headless) - 1:]) for cmd in made]  # without the program and its subcommand
    lines += [((host, "--help"), launch.launch_args(store, entry, kind, "session-1", "a-session-id"))
              for kind in ("researcher", "reviewer", "advisor")]
    checks = []
    for help_command, args in lines:
        checks += [(help_command, a) for a in args if a.startswith("-") and a != "-"]
        checks += [(help_command, args[i + 1]) for i, a in enumerate(args) if a in VALUED]
    return checks


def _herdr_checks() -> list[tuple[str, str, str]]:
    """(where, usage text, option) for the herdr commands rl runs; a group without a command lists its
    commands (bare herdr would open its interface)."""
    checks = []
    for group, commands in HERDR.items():
        text = _help("herdr", group)
        usage = {line.split()[2]: line for line in text.splitlines() if line.strip().startswith(f"herdr {group} ")}
        for name, options in commands.items():
            where = f"herdr {group} {name}"
            checks += [(where, usage[name], o) for o in options] if name in usage else [(where, "", name)]
        if group == "agent":
            kinds = re.search(r"kinds:\s*(\S+)", text)
            checks += [("herdr agent start --kind", kinds.group(1).replace("|", " ") if kinds else "", k)
                       for k in ("claude", "codex", "pi")]
    return checks


@pytest.mark.parametrize("host", ["claude", "codex", "pi", "herdr"])
def test_the_options_rl_gives_an_installed_host_are_in_its_help(host, project, monkeypatch):
    if not shutil.which(host):
        pytest.skip(f"{host} is not installed")
    if host == "herdr":
        checks = _herdr_checks()
    else:
        found = _host_checks(project, monkeypatch, host)
        helps = {c: _help(*c) for c in {c for c, _ in found}}
        checks = [(" ".join(c), helps[c], word) for c, word in found]
    missing = sorted({f"{where}: {word}" for where, text, word in checks if not _named(word, text)})
    assert not missing, f"the installed {host} no longer lists what rl gives it: " + "; ".join(missing)
