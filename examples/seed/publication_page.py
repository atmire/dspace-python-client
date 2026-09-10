"""
Publication-page example: one community, collection, item, and PDF bitstream from a fixed config.

The config format keeps metadata intentionally simple: each metadata key maps to either a
single string value or a list of string values. The script normalizes those values to the
DSpace REST payload shape automatically.

Example JSON config:

{
  "base_url": "http://localhost:8080",
  "community": {
    "name": "Publications"
  },
  "collection": {
    "name": "Publication Pages"
  },
  "item": {
    "name": "Example Publication Page",
    "metadata": {
      "dc.title": "Example Publication Page",
      "dc.contributor.author": [
        "Jane Doe",
        "John Doe"
      ],
      "dc.date.issued": "2026-04-23",
      "dc.identifier.uri": "https://example.org/publication/123"
    }
  },
  "bitstream": {
    "path": "examples/seed/sample.pdf",
    "name": "publication-page.pdf"
  }
}

If ``bitstream.path`` is omitted, the script uploads a generated sample PDF, matching the
MiniSpace default behavior.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from dspace_client import AuthenticationError, ServerVersionMismatchError, show_script_attribution
from dspace_client.exceptions import DSpaceAPIError

TARGET_VERSIONS = ["9.0", "10.0"]
SCRIPT_AUTHORS = "Abhinav Sidharthan (Atmire)"

_SEED_DIR = Path(__file__).resolve().parent
if str(_SEED_DIR) not in sys.path:
    sys.path.insert(0, str(_SEED_DIR))

from seed_client import connect_seed_client
from seed_data import DEFAULT_SEED_HTTP_TIMEOUT, DataFactory, load_seed_pack

console = Console()
DEFAULT_CONFIG = _SEED_DIR / "publication-page-config.json"
DEFAULT_SEEDPACK = _SEED_DIR / "seedpacks" / "default.yml"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file: {exc}") from exc


def _normalize_metadata(raw_metadata: Any) -> dict[str, list[dict[str, Any]]]:
    if raw_metadata is None:
        return {}
    if not isinstance(raw_metadata, dict):
        raise ValueError("'item.metadata' must be a JSON object")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for key, raw_value in raw_metadata.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Metadata keys must be non-empty strings")

        values = raw_value if isinstance(raw_value, list) else [raw_value]
        entries: list[dict[str, Any]] = []
        for value in values:
            if value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"Metadata field '{key}' must contain a string or list of strings")
            entries.append(
                {
                    "value": value,
                    "language": None,
                    "authority": None,
                    "confidence": -1,
                }
            )

        if entries:
            normalized[key] = entries

    return normalized


def _require_name(config: dict[str, Any], section: str) -> str:
    data = config.get(section)
    if not isinstance(data, dict):
        raise ValueError(f"Missing '{section}' section in config")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"'{section}.name' must be a non-empty string")
    return name.strip()


def _resolve_item_name(config: dict[str, Any], metadata: dict[str, list[dict[str, Any]]]) -> str:
    item = config.get("item")
    if not isinstance(item, dict):
        raise ValueError("Missing 'item' section in config")

    name = item.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()

    title_values = metadata.get("dc.title", [])
    if title_values:
        return str(title_values[0]["value"])

    raise ValueError("Provide 'item.name' or at least one 'dc.title' metadata value")


def _load_bitstream(
    config: dict[str, Any],
    item_name: str,
    factory: DataFactory,
) -> tuple[str, bytes]:
    bitstream = config.get("bitstream")
    if not isinstance(bitstream, dict):
        filename = "sample.pdf"
        content = factory.generate_sample_pdf_content(item_name)
        return filename, content

    path_value = bitstream.get("path")
    if isinstance(path_value, str) and path_value.strip():
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists() or not path.is_file():
            raise ValueError(f"Bitstream path does not exist or is not a file: {path}")
        filename = bitstream.get("name")
        if not isinstance(filename, str) or not filename.strip():
            filename = path.name
        return filename.strip(), path.read_bytes()

    filename = bitstream.get("name")
    if not isinstance(filename, str) or not filename.strip():
        filename = "sample.pdf"
    content = factory.generate_sample_pdf_content(item_name)
    return filename.strip(), content


async def run_publication_page(
    *,
    config_path: Path,
    base_url: str,
    username: str,
    password: str,
    strict_versions: bool,
) -> bool:
    if not DEFAULT_SEEDPACK.exists():
        console.print(f"[red]Seed pack not found: {DEFAULT_SEEDPACK}[/red]")
        return False

    try:
        seed_pack = load_seed_pack(DEFAULT_SEEDPACK)
        config = _read_json(config_path)
        community_name = _require_name(config, "community")
        collection_name = _require_name(config, "collection")
        metadata = _normalize_metadata(config.get("item", {}).get("metadata"))
        item_name = _resolve_item_name(config, metadata)
        factory = DataFactory(seed_pack, seed=42)
        bitstream_name, bitstream_content = _load_bitstream(config, item_name, factory)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        console.print(f"[red]{exc}[/red]")
        return False

    if not strict_versions:
        console.print(
            "[dim]Skipping server version probe (faster; use default behaviour to verify DSpace 9.0).[/dim]"
        )

    try:
        auth, client = await connect_seed_client(
            base_url=base_url,
            username=username,
            password=password,
            target_versions=TARGET_VERSIONS,
            strict_versions=strict_versions,
        )
    except ServerVersionMismatchError as exc:
        console.print(f"[red]Version mismatch:[/red] {exc}")
        return False
    except AuthenticationError as exc:
        console.print(f"[red]Authentication failed:[/red] {exc}")
        err = str(exc).lower()
        if "timeout" in err or "readtimeout" in err:
            console.print(
                "[yellow]The server did not respond in time. "
                f"This script uses a {int(DEFAULT_SEED_HTTP_TIMEOUT)}s HTTP timeout. "
                "Retry, or run against a faster server.[/yellow]"
            )
        return False

    try:
        anonymous_group = await client.search_group_by_name("Anonymous")
        if anonymous_group is None:
            console.print("[red]Could not find the built-in 'Anonymous' group.[/red]")
            return False

        preview = Table(
            title="Publication Page preview", show_header=True, header_style="bold cyan"
        )
        preview.add_column("Type", style="yellow")
        preview.add_column("Name", style="white")
        preview.add_row("Community", community_name)
        preview.add_row("Collection", collection_name)
        preview.add_row("Item", item_name)
        preview.add_row("Bitstream", bitstream_name)
        preview.add_row("Access", "Anonymous read")
        console.print()
        console.print(preview)
        console.print()

        metadata_table = Table(title="Metadata", show_header=True, header_style="bold cyan")
        metadata_table.add_column("Key", style="yellow")
        metadata_table.add_column("Value(s)", style="white")
        for key, values in metadata.items():
            metadata_table.add_row(key, " | ".join(str(entry["value"]) for entry in values))
        console.print()
        console.print(metadata_table)
        console.print()

        proceed = (
            console.input("\n[bold yellow]Proceed with creation? (yes/no):[/bold yellow] ")
            .strip()
            .lower()
        )
        if proceed not in ("yes", "y"):
            console.print("[yellow]Cancelled.[/yellow]")
            return True

        console.print("\n[bold cyan]Creating content…[/bold cyan]\n")

        community = await client.create_community(
            name=community_name,
            metadata={
                "dc.title": [
                    {"value": community_name, "language": None, "authority": None, "confidence": -1}
                ]
            },
        )
        console.print(f"  [green]✓[/green] Community: {community['uuid']}")

        collection = await client.create_collection(
            name=collection_name,
            parent_community_uuid=community["uuid"],
            metadata={
                "dc.title": [
                    {
                        "value": collection_name,
                        "language": None,
                        "authority": None,
                        "confidence": -1,
                    }
                ]
            },
        )
        console.print(f"  [green]✓[/green] Collection: {collection['uuid']}")

        item_read_group = await client.create_collection_item_read_group(
            collection["uuid"],
            description="Default item READ group for publication page imports",
        )
        bitstream_read_group = await client.create_collection_bitstream_read_group(
            collection["uuid"],
            description="Default bitstream READ group for publication page imports",
        )
        await client.add_subgroup_to_group(item_read_group["uuid"], anonymous_group["uuid"])
        await client.add_subgroup_to_group(bitstream_read_group["uuid"], anonymous_group["uuid"])
        console.print("  [green]✓[/green] Anonymous read access configured")

        item = await client.create_item(
            name=item_name,
            owning_collection_uuid=collection["uuid"],
            metadata=metadata,
        )
        console.print(f"  [green]✓[/green] Item: {item['uuid']}")

        bundle = await client.create_bundle(item["uuid"], "ORIGINAL")
        bitstream = await client.upload_bitstream(
            bundle_uuid=bundle["uuid"],
            filename=bitstream_name,
            content=bitstream_content,
        )
        console.print(f"  [green]✓[/green] Bitstream: {bitstream['uuid']}")

        console.print("\n[bold green]✓ Publication page content created.[/bold green]\n")

        summary = (
            f"[bold cyan]Created[/bold cyan]\n\n"
            f"[yellow]Community[/yellow]  {community['uuid']}\n"
            f"  {base_url.rstrip('/')}/communities/{community['uuid']}\n\n"
            f"[yellow]Collection[/yellow] {collection['uuid']}\n"
            f"  {base_url.rstrip('/')}/collections/{collection['uuid']}\n\n"
            f"[yellow]Item[/yellow]       {item['uuid']}\n"
            f"  {base_url.rstrip('/')}/items/{item['uuid']}\n\n"
            f"[yellow]Bitstream[/yellow]  {bitstream['uuid']}\n"
        )
        console.print()
        console.print(Panel(summary, title="Summary", border_style="green"))
        return True
    except DSpaceAPIError as exc:
        console.print(f"[red]DSpace API error:[/red] {exc}")
        return False
    finally:
        await auth.close()


async def main_async(args: argparse.Namespace) -> None:
    show_script_attribution(SCRIPT_AUTHORS, console=console)

    try:
        config = _read_json(DEFAULT_CONFIG.resolve())
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    supported = ", ".join(TARGET_VERSIONS)
    console.print(
        Panel.fit(
            "[bold cyan]Publication Page[/bold cyan]\n"
            "1 Community → 1 Collection → 1 Item → 1 Bitstream\n"
            "Config-driven metadata (key/value only)\n"
            "Collection defaults grant Anonymous READ on items and bitstreams\n"
            f"Target versions: {supported}",
            border_style="cyan",
        )
    )

    proceed = console.input("\n[bold yellow]Continue? (yes/no):[/bold yellow] ").strip().lower()
    if proceed not in ("yes", "y"):
        console.print("[dim]Cancelled.[/dim]")
        return

    base_url = args.base_url or config.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        base_url = console.input(
            "[bold cyan]DSpace base URL[/bold cyan] [dim](Enter for http://localhost:8080):[/dim] "
        ).strip()
    if not base_url:
        base_url = "http://localhost:8080"
        console.print("[dim]→ http://localhost:8080[/dim]")
    base_url = base_url.strip()

    is_demo = "demo.dspace.org" in base_url.rstrip("/").lower()
    if is_demo:
        console.print("[dim]Using demo admin credentials.[/dim]")
        username = "dspacedemo+admin@gmail.com"
        password = "dspace"
    else:
        username = console.input("[bold cyan]Username:[/bold cyan] ").strip()
        password = getpass.getpass("Password: ")

    ok = await run_publication_page(
        config_path=DEFAULT_CONFIG.resolve(),
        base_url=base_url,
        username=username,
        password=password,
        strict_versions=not args.skip_version_check,
    )
    if not ok:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a single publication page item from examples/seed/publication-page-config.json."
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=None,
        help="Override the base URL from the config file.",
    )
    parser.add_argument(
        "--skip-version-check",
        action="store_true",
        help="Skip verify_server_version after login (faster; default is to probe and enforce 9.0).",
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
