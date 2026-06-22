"""`rtd engagement ...` — CRUD on engagements + scope items.

Mirrors the HTTP surface in ``backend/app/api/engagements.py``. One sub-group
per concept: engagements themselves + nested scope items.
"""
from __future__ import annotations

import json
import sys

import click
from rich.table import Table

from rtd.output import emit, kv_table


@click.group(name="engagement")
def engagement_group() -> None:
    """List, create, view, and manage scope items for engagements."""


@engagement_group.command("list")
@click.option("--status", type=click.Choice(["active", "archived", "flushed"]),
              help="Filter by engagement status.")
@click.pass_context
def list_engagements(ctx: click.Context, status: str | None) -> None:
    """List engagements visible to this profile's API key."""
    params = {"status": status} if status else None
    with ctx.obj.client() as c:
        rows = c.get("/engagements", params=params)
    t = Table(title="Engagements")
    t.add_column("slug", style="bold")
    t.add_column("name")
    t.add_column("status")
    t.add_column("created")
    for r in rows:
        t.add_row(r["slug"], r["name"], r["status"], r["created_at"][:19])
    emit(rows, json_mode=ctx.obj.json_mode, table=t)


@engagement_group.command("create")
@click.option("--name", required=True, help="Human-readable engagement name.")
@click.option("--slug", help="Override the auto-generated slug.")
@click.pass_context
def create(ctx: click.Context, name: str, slug: str | None) -> None:
    """Create a new active engagement."""
    body: dict[str, str] = {"name": name}
    if slug:
        body["slug"] = slug
    with ctx.obj.client() as c:
        eng = c.post("/engagements", json=body)
    emit(
        eng,
        json_mode=ctx.obj.json_mode,
        table=kv_table(
            f"Created engagement {eng['slug']!r}",
            [("id", eng["id"]), ("slug", eng["slug"]), ("name", eng["name"]),
             ("status", eng["status"]), ("created_at", eng["created_at"])],
        ),
    )


@engagement_group.command("view")
@click.argument("slug")
@click.pass_context
def view(ctx: click.Context, slug: str) -> None:
    """Read one engagement by slug."""
    with ctx.obj.client() as c:
        eng = c.get(f"/engagements/{slug}")
    emit(
        eng,
        json_mode=ctx.obj.json_mode,
        table=kv_table(f"{slug}", [(k, eng.get(k)) for k in
                                   ["id", "slug", "name", "status", "created_at"]]),
    )


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

@engagement_group.group("scope")
def scope_group() -> None:
    """Manage scope items for an engagement."""


@scope_group.command("list")
@click.argument("slug")
@click.pass_context
def scope_list(ctx: click.Context, slug: str) -> None:
    """List scope items on engagement SLUG."""
    with ctx.obj.client() as c:
        rows = c.get(f"/engagements/{slug}/scope")
    t = Table(title=f"Scope ({slug})")
    t.add_column("kind", style="bold")
    t.add_column("value")
    t.add_column("exclude", justify="center")
    t.add_column("note")
    for r in rows:
        t.add_row(r["kind"], r["value"], "x" if r["is_exclusion"] else "", r.get("note") or "")
    emit(rows, json_mode=ctx.obj.json_mode, table=t)


@scope_group.command("add")
@click.argument("slug")
@click.option("--kind", required=True,
              type=click.Choice(["domain", "subdomain", "cidr", "ip", "url", "email"]),
              help="Scope item kind.")
@click.option("--value", required=True, help="Target value (e.g. 'acme.com', '10.0.0.0/24').")
@click.option("--exclude", is_flag=True,
              help="Mark as an exclusion — skipped even if covered by an include item.")
@click.option("--note", help="Free-form note (audit log only).")
@click.pass_context
def scope_add(
    ctx: click.Context,
    slug: str,
    kind: str,
    value: str,
    exclude: bool,
    note: str | None,
) -> None:
    """Add a scope item to engagement SLUG."""
    body: dict[str, object] = {"kind": kind, "value": value, "is_exclusion": exclude}
    if note:
        body["note"] = note
    with ctx.obj.client() as c:
        item = c.post(f"/engagements/{slug}/scope", json=body)
    emit(item, json_mode=ctx.obj.json_mode,
         table=kv_table("Scope item added",
                        [("id", item["id"]), ("kind", item["kind"]),
                         ("value", item["value"]), ("is_exclusion", item["is_exclusion"])]))


@scope_group.command("remove")
@click.argument("slug")
@click.argument("scope_id")
@click.pass_context
def scope_remove(ctx: click.Context, slug: str, scope_id: str) -> None:
    """Remove scope item SCOPE_ID from engagement SLUG."""
    with ctx.obj.client() as c:
        c.delete(f"/engagements/{slug}/scope/{scope_id}")
    from rtd.output import console
    console.print(f"removed scope item [bold]{scope_id}[/bold]")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@engagement_group.command("export")
@click.argument("slug")
@click.pass_context
def export_cmd(ctx: click.Context, slug: str) -> None:
    """Export engagement SLUG data to blob storage. Requires admin key."""
    with ctx.obj.client() as c:
        result = c.post(f"/engagements/{slug}/export")
    if result.get("blob_url"):
        from rtd.output import console
        console.print(f"exported: [bold]{result['blob_url']}[/bold]")
    else:
        emit(result, json_mode=ctx.obj.json_mode, table=kv_table(
            f"export ({slug})",
            [("slug", result.get("slug")), ("blob_url", result.get("blob_url") or "none — returned inline")],
        ))


@engagement_group.command("archive")
@click.argument("slug")
@click.pass_context
def archive_cmd(ctx: click.Context, slug: str) -> None:
    """Export and archive engagement SLUG. Requires admin key.

    Marks the engagement as done. It stays in the database but is excluded
    from active views. Reversible via PATCH /engagements/{slug}.
    """
    with ctx.obj.client() as c:
        result = c.delete(f"/engagements/{slug}")
    emit(
        result,
        json_mode=ctx.obj.json_mode,
        table=kv_table(
            f"archived {slug!r}",
            [("slug", result["slug"]), ("status", result["status"]),
             ("archived_at", result.get("archived_at"))],
        ),
    )


@engagement_group.command("flush")
@click.argument("slug")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def flush_cmd(ctx: click.Context, slug: str, yes: bool) -> None:
    """Permanently delete ALL data for engagement SLUG.

    An export is created in blob storage first (if configured).
    This cannot be undone.
    """
    if not yes:
        click.confirm(
            f"Permanently flush ALL data for engagement '{slug}'? This cannot be undone.",
            abort=True,
        )
    with ctx.obj.client() as c:
        c.post(f"/engagements/{slug}/flush")
    from rtd.output import console
    console.print(f"[bold red]flushed[/bold red] {slug!r}")


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------

# Display labels for phase enum values (internal API values -> user-visible labels)
PHASE_LABELS = {
    "osint": "OSINT",
    "vuln_scan": "Vulnerability Scan",
    "exploit": "Validation",
    "phishing": "Phishing",
    "general": "General",
}


@engagement_group.group("observations")
def observations_group() -> None:
    """Manage freeform observations for an engagement."""


@observations_group.command("list")
@click.argument("slug")
@click.pass_context
def observations_list(ctx: click.Context, slug: str) -> None:
    """List observations for engagement SLUG."""
    with ctx.obj.client() as c:
        rows = c.get(f"/engagements/{slug}/observations")
    t = Table(title=f"Observations ({slug})")
    t.add_column("id", style="dim")
    t.add_column("phase")
    t.add_column("content")
    t.add_column("created")
    for r in rows:
        phase_display = PHASE_LABELS.get(r.get("phase"), r.get("phase") or "")
        t.add_row(
            r["id"][:8],
            phase_display,
            r["content"][:80] + ("…" if len(r["content"]) > 80 else ""),
            r["created_at"][:19],
        )
    emit(rows, json_mode=ctx.obj.json_mode, table=t)


@observations_group.command("add")
@click.argument("slug")
@click.argument("content")
@click.option(
    "--phase",
    type=click.Choice(list(PHASE_LABELS.keys())),
    help=f"Optional phase tag: {', '.join(PHASE_LABELS.values())}.",
)
@click.pass_context
def observations_add(ctx: click.Context, slug: str, content: str, phase: str | None) -> None:
    """Add an observation to engagement SLUG."""
    body: dict[str, object] = {"content": content}
    if phase:
        body["phase"] = phase
    with ctx.obj.client() as c:
        obs = c.post(f"/engagements/{slug}/observations", json=body)
    emit(
        obs,
        json_mode=ctx.obj.json_mode,
        table=kv_table(
            "Observation added",
            [("id", obs["id"]), ("phase", obs.get("phase")), ("created_at", obs["created_at"])],
        ),
    )


@observations_group.command("delete")
@click.argument("observation_id")
@click.pass_context
def observations_delete(ctx: click.Context, observation_id: str) -> None:
    """Delete observation OBSERVATION_ID."""
    with ctx.obj.client() as c:
        c.delete(f"/observations/{observation_id}")
    from rtd.output import console
    console.print(f"deleted observation [bold]{observation_id}[/bold]")


# ---------------------------------------------------------------------------
# Findings import
# ---------------------------------------------------------------------------


@engagement_group.command("import-findings")
@click.argument("slug")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def import_findings_cmd(ctx: click.Context, slug: str, file: str) -> None:
    """Import findings from FILE (JSON array) into engagement SLUG.

    All imported findings land as pending_validation for analyst review.

    FILE must be a JSON array where each object has at minimum a ``title``
    field. Optional fields: severity, phase, summary, target, source_tool,
    details.

    Example:
        rtd engagement import-findings my-eng findings.json
    """
    with open(file) as fh:
        try:
            payload = json.load(fh)
        except json.JSONDecodeError as exc:
            from rtd.output import console
            console.print(f"[red]invalid JSON:[/red] {exc}")
            sys.exit(1)

    if not isinstance(payload, list):
        from rtd.output import console
        console.print("[red]FILE must contain a JSON array of findings[/red]")
        sys.exit(1)

    with ctx.obj.client() as c:
        findings = c.post(f"/engagements/{slug}/findings/import", json=payload)

    t = Table(title=f"Imported findings → {slug}")
    t.add_column("title")
    t.add_column("severity", style="bold")
    t.add_column("phase")
    t.add_column("target")
    for f in findings:
        phase_display = PHASE_LABELS.get(f.get("phase"), f.get("phase", ""))
        t.add_row(f["title"][:60], f["severity"], phase_display, f.get("target") or "")
    from rtd.output import console
    emit(findings, json_mode=ctx.obj.json_mode, table=t)
    if not ctx.obj.json_mode:
        console.print(f"[green]{len(findings)} finding(s) imported as pending_validation[/green]")
