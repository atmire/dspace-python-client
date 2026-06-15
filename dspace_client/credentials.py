"""Interactive credential helpers for example scripts."""

from __future__ import annotations

import getpass

from rich.console import Console

from .auth import DSpaceAuthClient
from .exceptions import AuthenticationError

_DEFAULT_MAX_ATTEMPTS = 3


async def prompt_and_authenticate(
    auth: DSpaceAuthClient,
    username: str,
    password: str,
    *,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    password_prompt: str = "Admin password: ",
    console: Console | None = None,
) -> tuple[str, dict]:
    """
    Authenticate with up to ``max_attempts`` tries when credentials are rejected.

    On HTTP 401/403 from login, prints a short message and re-prompts for the
    password only. Other authentication failures (CSRF, network, verification)
    are raised immediately without retry.

    Args:
        auth: Configured :class:`DSpaceAuthClient` (not yet authenticated).
        username: DSpace username or email.
        password: First password to try (from an earlier prompt or demo default).
        max_attempts: Give up after this many failed credential attempts.
        password_prompt: ``getpass`` label when asking again.
        console: Rich console for user messages (optional).

    Returns:
        ``(jwt_token, status_dict)`` from :meth:`DSpaceAuthClient.authenticate`.

    Raises:
        AuthenticationError: After ``max_attempts`` credential failures, or on
            non-credential auth errors.
    """
    out = console
    current_password = password

    for attempt in range(1, max_attempts + 1):
        try:
            return await auth.authenticate(username, current_password)
        except AuthenticationError as exc:
            if not exc.credential_failure:
                raise
            if attempt >= max_attempts:
                raise AuthenticationError(
                    "Login failed after "
                    f"{max_attempts} attempts. Check your username and password, "
                    "then run the script again.",
                    status_code=exc.status_code,
                ) from None
            if out is not None:
                remaining = max_attempts - attempt
                out.print(
                    "[yellow]Incorrect password.[/yellow] "
                    f"Please try again ({remaining} "
                    f"{'try' if remaining == 1 else 'tries'} left)."
                )
            current_password = getpass.getpass(password_prompt)

    raise AuthenticationError("Login failed.")
