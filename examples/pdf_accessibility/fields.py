"""Make sure a metadata field can be written: check it, or create it after confirmation."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel

from .session import Session, ask
from .sinks import split_field

DEFAULT_FIELD = "local.accessibility.report"


async def prepare_metadata_field(
    console: Console,
    session: Session,
    field_name: str,
    apply: bool,
    *,
    purpose: str,
    scope_note: str,
    replaces: str,
) -> bool:
    """
    Check that ``field_name`` exists (or can be created) and, with ``apply``, get confirmation.

    The schema must already exist: this example creates fields, never schemas. When the
    field is missing and ``apply`` is set, it is created only after the user retypes its name.
    Returns False when the run should stop.
    """
    try:
        schema, element, qualifier = split_field(field_name)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return False
    client = session.client
    schema_obj: dict[str, Any] | None = None
    if await client.find_metadata_field(field_name):
        console.print(f"Metadata field [bold]{field_name}[/bold] exists.")
        exists = True
    else:
        schema_obj = await client.find_metadata_schema(schema)
        if not schema_obj:
            console.print(
                f"[red]The metadata schema '{schema}' does not exist.[/red] This script only creates "
                "fields, not schemas. Create the schema first (Admin > Registries > Metadata), or "
                f"choose a field in an existing schema such as '{DEFAULT_FIELD}'."
            )
            return False
        exists = False
        console.print(
            f"Metadata field [bold]{field_name}[/bold] does not exist yet; it will be created in schema '{schema}'."
        )

    if not apply:
        console.print(
            "[yellow]Dry run:[/yellow] nothing is written to the repository. Re-run with --apply "
            "to write the planned changes."
        )
        return True

    action = purpose if exists else f"CREATE and then {purpose}"
    console.print(
        Panel.fit(
            f"This run will {action} [bold]{field_name}[/bold].\n{replaces}\n"
            + (
                ""
                if exists
                else "Creating a field changes the metadata registry; deleting it later deletes all its values.\n"
            )
            + "Make sure the field is hidden from the public (see README) before running on production.",
            title="Confirm repository changes",
            border_style="red",
        )
    )
    typed = ask(console, f"Type the field name ({field_name}) to confirm")
    if typed != field_name:
        console.print("[yellow]Not confirmed; stopping.[/yellow]")
        return False
    if not exists and schema_obj is not None:
        created = await client.create_metadata_field(
            int(schema_obj["id"]), element, qualifier, scope_note=scope_note
        )
        console.print(
            f"[green]Created metadata field {field_name} (id {created.get('id')}).[/green]"
        )
    return True
