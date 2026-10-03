"""
Login, version check, admin check and session upkeep shared by detect.py and remediate.py.

Both scripts need a site administrator: only a site admin can read every bitstream
(embargoed and restricted ones, and those of in-progress submissions), list every workspace
and workflow item, and add fields to the metadata registry.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from typing import Any

from rich.console import Console

from dspace_client import (
    AuthenticationError,
    DSpaceAuthClient,
    DSpaceClient,
    ServerVersionMismatchError,
)

# DEVELOPER DECLARES: written and tested for DSpace 9 and 10 (the REST endpoints used here
# exist since DSpace 7, but older versions are not supported by this example).
TARGET_VERSIONS = ["9.0", "10.0"]
SCRIPT_AUTHORS = "Bram Luyten (Atmire)"

# Sent on every request, so the audit's downloads can be excluded from usage statistics by
# listing this agent in [dspace]/config/spiders/agents (see README).
LOGIN_ATTEMPTS = 3

USER_AGENT = "dspace-a11y-audit/0.1 (+https://github.com/atmire/dspace-python-client)"


@dataclass
class Session:
    auth: DSpaceAuthClient
    client: DSpaceClient
    username: str
    password: str
    eperson: dict[str, Any]

    @property
    def eperson_uuid(self) -> str:
        return str(self.eperson["uuid"])

    async def refresh(self) -> None:
        """Re-authenticate when the session is near its maximum age; sync the client's tokens."""
        jwt = await self.auth.ensure_session(self.username, self.password)
        self.client.jwt_token = self.auth.jwt_token or jwt
        if self.auth.csrf_token:
            self.client.csrf_token = self.auth.csrf_token

    async def close(self) -> None:
        await self.auth.close()


def ask(console: Console, prompt: str, default: str | None = None) -> str:
    hint = f" [dim](Enter for {default})[/dim]" if default else ""
    value = console.input(f"[bold cyan]{prompt}[/bold cyan]{hint}: ").strip()
    return value or (default or "")


async def open_admin_session(
    console: Console,
    base_url: str | None,
    username: str | None,
    *,
    skip_version_check: bool = False,
    courtesy_delay: float = 0.1,
) -> Session | None:
    """
    Log in, check the server version and confirm the account is a site administrator.

    Returns ``None`` (after printing why) when any step fails.
    """
    base_url = (base_url or ask(console, "DSpace base URL (e.g. https://repo.example.org)")).rstrip(
        "/"
    )
    base_url = base_url.removesuffix("/server")
    username = username or ask(console, "Admin username (email)")
    password = getpass.getpass("Admin password: ")

    auth = DSpaceAuthClient(base_url)
    auth.show_atmire_promo = True
    # Same retry behaviour as dspace_client.prompt_and_authenticate, but we keep the password
    # that worked: a long run needs it to log in again if refreshing the token fails.
    jwt = None
    for attempt in range(1, LOGIN_ATTEMPTS + 1):
        try:
            jwt, _status = await auth.authenticate(username, password)
            break
        except AuthenticationError as exc:
            if not exc.credential_failure or attempt == LOGIN_ATTEMPTS:
                console.print(f"[red]Login failed:[/red] {exc}")
                await auth.close()
                return None
            console.print(
                f"[yellow]Incorrect password.[/yellow] {LOGIN_ATTEMPTS - attempt} tries left."
            )
            password = getpass.getpass("Admin password: ")

    if auth.client is not None:
        auth.client.headers["User-Agent"] = USER_AGENT

    client = DSpaceClient(
        base_url=base_url,
        jwt_token=jwt,
        csrf_token=auth.csrf_token,
        http_client=auth.client,
        target_versions=TARGET_VERSIONS,
        courtesy_delay=courtesy_delay,
    )
    if skip_version_check:
        console.print("[yellow]Skipping the server version check (--skip-version-check).[/yellow]")
    else:
        try:
            await client.verify_server_version(raise_on_mismatch=True)
        except ServerVersionMismatchError as exc:
            console.print(
                f"[red]{exc}[/red]\nThis script supports DSpace {', '.join(TARGET_VERSIONS)}. "
                "Use --skip-version-check only if you know the REST API is compatible."
            )
            await auth.close()
            return None

    eperson = await client.get_current_eperson()
    if not eperson:
        console.print("[red]Could not determine the logged-in user.[/red]")
        await auth.close()
        return None
    if not await client.is_site_admin(eperson["uuid"]):
        console.print(
            f"[red]{eperson.get('email', username)} is not a site administrator.[/red] "
            "This script needs a site (repository-wide) administrator account, so that it can "
            "read every bitstream, including embargoed files and in-progress submissions."
        )
        await auth.close()
        return None
    console.print(
        f"[green]Logged in as site administrator {eperson.get('email', username)}.[/green]"
    )
    return Session(auth=auth, client=client, username=username, password=password, eperson=eperson)
