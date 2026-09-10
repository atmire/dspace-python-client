"""Centralized exceptions for DSpace client."""


class DSpaceClientError(Exception):
    """Base exception for all DSpace client errors."""


class AuthenticationError(DSpaceClientError):
    """Raised when authentication fails."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def credential_failure(self) -> bool:
        """True when the failure is likely wrong username/password (HTTP 401 or 403)."""
        return self.status_code in (401, 403)


class DSpaceAPIError(DSpaceClientError):
    """Raised when DSpace API returns an error.

    Attributes:
        status_code: Optional HTTP status code from the response, if available.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class VersionIncompatibilityError(DSpaceClientError):
    """Raised when an operation is not compatible with target DSpace version(s)."""

    def __init__(
        self,
        operation: str,
        target_versions: list[str],
        supported_versions: list[str],
        message: str | None = None,
    ):
        self.operation = operation
        self.target_versions = target_versions
        self.supported_versions = supported_versions

        if message is None:
            missing_versions = set(target_versions) - set(supported_versions)
            message = (
                f"Operation '{operation}' not supported in DSpace version(s): {', '.join(missing_versions)}. "
                f"Supported versions: {', '.join(supported_versions)}. "
                f"Consider using target_versions={supported_versions} or implement workaround."
            )

        super().__init__(message)


class DocumentationError(DSpaceClientError):
    """Raised when documentation fetching or validation fails."""


class NetworkError(DSpaceClientError):
    """Raised when network operations fail."""


class ServerVersionMismatchError(DSpaceClientError):
    """Raised when server version doesn't match target_versions."""

    def __init__(self, server_version: str, target_versions: list[str], message: str | None = None):
        self.server_version = server_version
        self.target_versions = (
            target_versions if isinstance(target_versions, list) else [target_versions]
        )

        if message is None:
            target_versions_str = ", ".join(self.target_versions)
            message = (
                f"Server version {server_version} is not compatible with target version(s) {target_versions_str}. "
                f"Major version mismatch detected. Please connect to a server running one of the target versions."
            )

        super().__init__(message)


class OAIError(DSpaceClientError):
    """Raised when an OAI-PMH repository returns an error response."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or code
        super().__init__(f"OAI-PMH error: {self.code} - {self.message}")
