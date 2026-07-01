"""2B command-line entry point (Milestone 1: one-shot task execution).

Preserves the prototype's one-shot usage and flags. The persistent REPL loop,
slash commands, and multi-task session arrive in Milestone 2; keeping the
one-shot path here means `2b "task"` behaves exactly like the prototype did,
just with the live status-line TUI attached.
"""
import argparse

from rich.console import Console

from . import __version__, orchestrator
from .tui import StatusTUI


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="2b",
        description="A local-first coding agent that keeps small models focused.",
    )
    parser.add_argument("--model", help="Ollama model tag, e.g. qwen3.5:9b (default: autodetect)")
    parser.add_argument("--yes", action="store_true", help="Auto-apply file writes/edits without confirmation")
    parser.add_argument("--list-models", action="store_true", help="List installed Ollama models and exit")
    parser.add_argument("--version", action="version", version=f"2b {__version__}")
    parser.add_argument("task", nargs="?", help="The task to give the agent")
    args = parser.parse_args()

    console = Console()

    if args.list_models:
        try:
            models = orchestrator.list_installed_models()
        except Exception as e:
            console.print(f"[red]Could not reach Ollama at {orchestrator.ollama_host()}: {e}[/red]")
            raise SystemExit(1)
        if not models:
            console.print(f"No models installed in Ollama at {orchestrator.ollama_host()}.")
        else:
            for m in models:
                marker = "  (default)" if m == orchestrator.DEFAULT_MODEL else ""
                console.print(f"{m}{marker}")
        raise SystemExit(0)

    if not args.task:
        parser.error("a task is required unless --list-models is given")

    model = args.model or orchestrator.pick_default_model()
    if not args.model:
        console.print(f"[dim]No --model given, autodetected: {model}[/dim]")

    tui = StatusTUI(console=console)
    try:
        orchestrator.run_task(
            model=model,
            task_text=args.task,
            auto_yes=args.yes,
            on_event=tui.handle,
        )
    finally:
        tui.close()


if __name__ == "__main__":
    main()
