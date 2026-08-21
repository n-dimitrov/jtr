from __future__ import annotations

from collections.abc import Callable
from typing import Any

import typer
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text

from . import audit


class WriteCancelled(Exception):
    pass


def render_field_diff(label: str, old: str, new: str) -> RenderableType:
    t = Text()
    t.append(f"{label}: ", style="bold")
    t.append(old or "—", style="red strike" if old else "dim")
    t.append("  →  ", style="dim")
    t.append(new or "—", style="green")
    return t


def confirm_apply(
    console: Console,
    *,
    action: str,
    key: str,
    preview: RenderableType,
    apply_fn: Callable[[], Any],
    before: Any = None,
    after: Any = None,
    assume_yes: bool = False,
    quiet: bool = False,
) -> dict:
    """Render a preview, prompt for confirmation, apply, and audit.

    - assume_yes skips the prompt (for `--yes` flag wiring at the CLI layer).
    - quiet suppresses the preview, so `--json` callers get a clean stdout.
    - Cancellation raises WriteCancelled; nothing is audited (per plan).
    - On apply: success and failure both append an audit line.

    Returns the audit row that was appended.
    """
    if not quiet:
        console.print(Panel(preview, title=f"{action}  {key}", expand=False))

    if not assume_yes and not typer.confirm("Apply?", default=False):
        if not quiet:
            console.print("[yellow]Cancelled — no change made.[/]")
        raise WriteCancelled()

    try:
        result = apply_fn()
    except Exception as e:
        audit.append(
            action=action,
            key=key,
            ok=False,
            before=before,
            after=after,
            error=f"{type(e).__name__}: {e}",
        )
        raise

    return audit.append(
        action=action,
        key=key,
        ok=True,
        before=before,
        after=after,
        result=_jsonable(result),
    )


def _jsonable(v: Any) -> Any:
    """Best-effort: keep simple JSON; stringify exotic objects."""
    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    return str(v)
