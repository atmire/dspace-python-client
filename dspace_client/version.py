"""Version compatibility system for DSpace client."""

import re
from dataclasses import dataclass
from typing import ClassVar

from .exceptions import VersionIncompatibilityError
from .versions import SUPPORTED_VERSIONS as _SUPPORTED_VERSIONS


def _extract_major_minor(version_str: str) -> str | None:
    """
    Extract a major.minor version substring from a free-form version string.

    Examples:
    - "7.6" -> "7.6"
    - "DSpace 7.6" -> "7.6"
    - "9.0.1" -> "9.0"
    - "Version 9.0.1 (build 123)" -> "9.0"
    """
    if not version_str or not isinstance(version_str, str):
        return None
    # Find the first major.minor pattern, e.g. "7.6" in "DSpace 7.6" or "9.0" in "9.0.1"
    match = re.search(r"(\d+)\.(\d+)", version_str)
    if not match:
        return None
    return f"{match.group(1)}.{match.group(2)}"


@dataclass
class DSpaceVersion:
    """Represents a DSpace version."""
    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"

    @classmethod
    def from_string(cls, version_str: str) -> "DSpaceVersion":
        """
        Create DSpaceVersion from string like '7.6', '8.0', or '9.0.1'.
        Patch versions are ignored (9.0.1 -> 9.0).
        """
        try:
            # Handle patch versions (e.g., "9.0.1" -> "9.0")
            parts = version_str.split(".")
            major = int(parts[0])
            minor = int(parts[1]) if len(parts) > 1 else 0
            return cls(major, minor)
        except (ValueError, IndexError):
            raise ValueError(f"Invalid version string: {version_str}")

    @property
    def version_string(self) -> str:
        """Get version as string."""
        return str(self)


class VersionCompatibility:
    """
    Validates API operations against target DSpace version(s).
    
    CRITICAL: Every API call is validated before execution.
    """

    SUPPORTED_VERSIONS = _SUPPORTED_VERSIONS

    # Method compatibility matrix
    # Each method lists the minimum version that supports it
    COMPATIBILITY: ClassVar[dict[str, list[str]]] = {
        # Core CRUD operations (available in all versions)
        "create_community": ["7.0+"],
        "delete_community": ["7.0+"],
        "create_collection": ["7.0+"],
        "delete_collection": ["7.0+"],
        "create_item": ["7.0+"],
        "delete_item": ["7.0+"],
        "create_bundle": ["7.0+"],
        "upload_bitstream": ["7.0+"],
        "delete_bitstream": ["7.0+"],

        # EPerson operations
        "create_eperson": ["7.0+"],
        "delete_eperson": ["7.0+"],
        "add_eperson_to_group": ["7.0+"],

        # Group operations
        "create_group": ["7.0+"],
        "delete_group": ["7.0+"],
        "search_group_by_name": ["7.0+"],
        "find_or_create_group": ["7.0+"],
        "add_subgroup_to_group": ["7.0+"],

        # Collection default groups
        "create_collection_item_read_group": ["7.0+"],
        "create_collection_bitstream_read_group": ["7.0+"],

        # Statistics
        "create_item_view": ["7.0+"],

        # Read operations
        "get_item_bundles": ["7.0+"],
        "get_bundle_bitstreams": ["7.0+"],
        "get_bitstream_format": ["7.0+"],
        "get_bitstream_formats": ["7.0+"],
        "search_items": ["7.0+"],
        "get_item": ["7.0+"],
        "patch_item": ["7.0+"],
        "get_vocabulary_entries": ["7.0+"],
        "get_vocabulary_entry_detail": ["7.0+"],
        "get_eperson": ["7.0+"],

        # Version-specific methods are expressed by raising the floor, e.g. a method
        # only available from DSpace 8 onwards would be listed as ["8.0+"].
    }

    def __init__(self, target_versions: str | list[str], docs_fetcher=None):
        """
        Initialize validator with target versions.
        
        Args:
            target_versions: Single version string or list of versions to validate against
            docs_fetcher: Optional fetcher with loaded REST contract docs
        """
        if isinstance(target_versions, str):
            self.target_versions = [target_versions]
        else:
            self.target_versions = target_versions

        self.docs_fetcher = docs_fetcher

        # Validate that all target versions are supported
        for version in self.target_versions:
            if version not in self.SUPPORTED_VERSIONS:
                raise ValueError(f"Unsupported DSpace version: {version}")

    def validate_before_call(self, method_name: str, endpoint: str, operation: str) -> None:
        """
        Validate operation is compatible with ALL target versions.
        
        Called automatically before every API call.
        
        Args:
            method_name: Name of the method being called
            endpoint: API endpoint path
            operation: HTTP operation (GET, POST, PUT, DELETE)
        
        Raises:
            VersionIncompatibilityError: If operation not supported in any target version
        """
        # Check if method is in compatibility matrix
        if method_name not in self.COMPATIBILITY:
            # If not in matrix, assume it's supported in all versions
            return

        required_versions = self.COMPATIBILITY[method_name]

        # Check if any target version is incompatible
        incompatible_versions = []
        for target_version in self.target_versions:
            if not self._is_version_compatible(target_version, required_versions):
                incompatible_versions.append(target_version)

        if incompatible_versions:
            # Find which versions DO support this operation
            supported_versions = []
            for version in self.target_versions:
                if self._is_version_compatible(version, required_versions):
                    supported_versions.append(version)

            raise VersionIncompatibilityError(
                operation=method_name,
                target_versions=self.target_versions,
                supported_versions=supported_versions,
                message=(
                    f"Operation '{method_name}' not supported in DSpace version(s): {', '.join(incompatible_versions)}. "
                    f"Supported in: {', '.join(supported_versions) if supported_versions else 'none'}. "
                    f"Consider using target_versions={supported_versions} or implement workaround."
                )
            )

    def _is_version_compatible(self, version: str, required_versions: list[str]) -> bool:
        """Check if a version is compatible with required versions."""
        if version == "bleeding-edge":
            # Bleeding edge supports everything
            return True

        # Check if version matches any requirement
        for required in required_versions:
            if required.endswith("+"):
                # Version range like "7.0+" means 7.0 and above
                min_version = required[:-1]
                if self._version_compare(version, min_version) >= 0:
                    return True
            elif version == required:
                # Exact version match
                return True

        return False

    def _version_compare(self, version1: str, version2: str) -> int:
        """Compare two version strings. Returns -1, 0, or 1."""
        try:
            v1 = DSpaceVersion.from_string(version1)
            v2 = DSpaceVersion.from_string(version2)

            if v1.major != v2.major:
                return -1 if v1.major < v2.major else 1
            if v1.minor != v2.minor:
                return -1 if v1.minor < v2.minor else 1
            return 0
        except ValueError:
            # If version parsing fails, assume incompatible
            return -1

    def get_compatibility_report(self) -> dict[str, list[str]]:
        """Generate report of which operations work with which versions."""
        report = {}

        for method_name, required_versions in self.COMPATIBILITY.items():
            compatible_versions = []
            for target_version in self.target_versions:
                if self._is_version_compatible(target_version, required_versions):
                    compatible_versions.append(target_version)

            report[method_name] = compatible_versions

        return report

    def get_incompatible_operations(self) -> dict[str, list[str]]:
        """Get operations that are incompatible with current target versions."""
        incompatible = {}

        for method_name, required_versions in self.COMPATIBILITY.items():
            incompatible_versions = []
            for target_version in self.target_versions:
                if not self._is_version_compatible(target_version, required_versions):
                    incompatible_versions.append(target_version)

            if incompatible_versions:
                incompatible[method_name] = incompatible_versions

        return incompatible

    @staticmethod
    def check_server_version_compatibility(
        server_version: str,
        target_versions: list[str]
    ) -> tuple[bool, str | None]:
        """
        Check if server version is compatible with target versions.
        
        Compatibility rules:
        - Exact match (e.g., 9.0 == 9.0) → OK, no warning
        - Minor version difference, same major (e.g., 9.0 vs 9.1) → OK with warning
        - Major version difference (e.g., 7.x vs 8.0+) → NOT compatible
        
        Args:
            server_version: Actual server version as reported by the server. This may be a
                free-form string like "DSpace 7.6" or "9.0.1 (build ...)".
            target_versions: List of target versions (e.g., ["8.0", "9.0"])
        
        Returns:
            Tuple of (is_compatible: bool, warning_message: Optional[str])
            - If major version mismatch: (False, None)
            - If exact match: (True, None)
            - If minor version difference: (True, warning_message)
        """
        # Normalize the server version into a major.minor string where possible, while
        # keeping the original string for messages.
        normalized_server_version = _extract_major_minor(server_version) or server_version
        if "bleeding-edge" in target_versions:
            # Bleeding-edge allows any version, but warn if server is old
            try:
                server_v = DSpaceVersion.from_string(normalized_server_version)
                if server_v.major < 7:
                    return True, f"Server version {server_version} is quite old. Proceeding with caution."
                return True, None
            except ValueError:
                # If we can't parse, allow it but warn
                return True, f"Could not parse server version '{server_version}'. Proceeding with caution."

        try:
            server_v = DSpaceVersion.from_string(normalized_server_version)
        except ValueError:
            # If we can't parse server version, we can't validate
            return True, f"Could not parse server version '{server_version}'. Version validation skipped."

        # Check each target version
        exact_match = False
        same_major = False
        target_majors = set()

        for target_version in target_versions:
            try:
                target_v = DSpaceVersion.from_string(target_version)
                target_majors.add(target_v.major)

                if server_v.major == target_v.major and server_v.minor == target_v.minor:
                    exact_match = True
                elif server_v.major == target_v.major:
                    same_major = True
            except ValueError:
                # Skip invalid target versions (shouldn't happen, but handle gracefully)
                continue

        # Check for major version mismatch
        if server_v.major not in target_majors:
            return False, None

        # Exact match - no warning
        if exact_match:
            return True, None

        # Same major, different minor - warn but allow
        if same_major:
            target_versions_str = ", ".join(target_versions)
            return True, (
                f"Server version {server_version} differs from target version(s) {target_versions_str} "
                f"(same major version, different minor). Proceeding with caution."
            )

        # Should not reach here, but allow if we do
        return True, None
