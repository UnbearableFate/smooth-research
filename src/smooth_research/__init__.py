"""Smooth Research: a lean research-records core shared by the Claude Code and Codex plugins."""

__version__ = "0.1.0"


def main() -> int:
    from .cli import main as cli_main
    return cli_main()
