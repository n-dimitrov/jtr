from __future__ import annotations

import json
import sys
from dataclasses import asdict

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import Comment, Project, SearchPage, Ticket


def _short_date(s: str) -> str:
    # Jira returns "2026-06-12T14:23:01.000+0200" — keep first 16 chars.
    return s[:16].replace("T", " ") if s else ""


_STATUS_STYLES = {
    "open": "yellow",
    "to do": "yellow",
    "in progress": "cyan",
    "in review": "cyan",
    "blocked": "red",
    "done": "green",
    "closed": "green",
    "resolved": "green",
}


def _status_style(name: str) -> str:
    return _STATUS_STYLES.get(name.lower(), "white")


def render_ticket_table(console: Console, page: SearchPage) -> None:
    tickets = page.tickets
    if not tickets:
        console.print("[dim]No tickets.[/]")
        return
    t = Table(show_lines=False, header_style="bold")
    t.add_column("Key", style="bold")
    t.add_column("Status")
    t.add_column("Priority")
    t.add_column("Assignee")
    t.add_column("Updated")
    t.add_column("Summary")
    for tk in tickets:
        t.add_row(
            tk.key,
            Text(tk.status, style=_status_style(tk.status)),
            tk.priority,
            tk.assignee.short() if tk.assignee else "[dim]—[/]",
            _short_date(tk.updated),
            tk.summary,
        )
    console.print(t)
    if page.total is None:
        # Cloud's cursor-paged search reports no total, so there is no
        # "x of y" to print — claiming one would be inventing it.
        console.print(f"[dim]{len(tickets)} ticket(s)[/]")
    else:
        shown = f"{page.start_at + 1}-{page.start_at + len(tickets)}"
        console.print(f"[dim]{shown} of {page.total} ticket(s)[/]")
    if page.next_start_at is not None:
        console.print(f"[dim]More: re-run with --start-at {page.next_start_at}[/]")
    elif page.next_page_token:
        console.print(f"[dim]More: re-run with --cursor {page.next_page_token}[/]")


def render_ticket_detail(
    console: Console, ticket: Ticket, comments: list[Comment]
) -> None:
    header = Text()
    header.append(f"{ticket.key}  ", style="bold")
    header.append(ticket.summary)
    header.append(f"\n{ticket.issue_type}  ", style="dim")
    header.append(ticket.status, style=_status_style(ticket.status))
    if ticket.priority:
        header.append(f"  •  {ticket.priority}", style="dim")
    console.print(Panel(header, expand=False))

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim")
    meta.add_column()
    meta.add_row("Assignee", ticket.assignee.short() if ticket.assignee else "—")
    meta.add_row("Reporter", ticket.reporter.short() if ticket.reporter else "—")
    meta.add_row("Project", ticket.project_key or "—")
    meta.add_row("Created", _short_date(ticket.created))
    meta.add_row("Updated", _short_date(ticket.updated))
    if ticket.labels:
        meta.add_row("Labels", ", ".join(ticket.labels))
    if ticket.fix_versions:
        meta.add_row("Fix versions", ", ".join(ticket.fix_versions))
    console.print(meta)

    if ticket.description.strip():
        console.print()
        console.print(Panel(ticket.description, title="Description", expand=True))

    if comments:
        console.print()
        items = []
        for c in comments[-10:]:
            author = c.author.short() if c.author else "unknown"
            head = Text(f"{author}  ", style="bold")
            head.append(f"· {_short_date(c.created)}", style="dim")
            items.append(Panel(Group(head, Text(""), Text(c.body)), expand=True))
        console.print(Panel(Group(*items), title=f"Comments ({len(comments)})"))
    else:
        console.print()
        console.print("[dim]No comments.[/]")


def print_ticket_table_json(page: SearchPage, jql: str) -> None:
    payload = {
        "jql": jql,
        "count": len(page.tickets),
        "start_at": page.start_at,
        "max_results": page.max_results,
        # null on Cloud, which reports no total for a search.
        "total": page.total,
        "next_start_at": page.next_start_at,
        "next_page_token": page.next_page_token,
        "has_more": page.has_more,
        "tickets": [asdict(t) for t in page.tickets],
    }
    print_json(payload)


def render_project_table(console: Console, projects: list[Project]) -> None:
    if not projects:
        console.print("[dim]No projects visible.[/]")
        return
    t = Table(show_lines=False, header_style="bold")
    t.add_column("Key", style="bold")
    t.add_column("Name")
    t.add_column("Lead")
    for p in projects:
        t.add_row(p.key, p.name, p.lead or "[dim]—[/]")
    console.print(t)
    console.print(f"[dim]{len(projects)} project(s)[/]")


def print_projects_json(projects: list[Project]) -> None:
    print_json({
        "count": len(projects),
        "projects": [asdict(p) for p in projects],
    })


def print_json(payload: dict) -> None:
    """The one place a machine-readable payload reaches stdout."""
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def print_ticket_detail_json(ticket: Ticket, comments: list[Comment]) -> None:
    print_json({
        "ticket": asdict(ticket),
        "comments": [asdict(c) for c in comments],
    })


def print_json_error(code: str, message: str, fix: str | None = None) -> None:
    """Emit a structured error to stdout for --json consumers."""
    payload: dict = {"error": code, "message": message}
    if fix:
        payload["fix"] = fix
    print_json(payload)
