from __future__ import annotations


class SubctlError(Exception):
    """Base class for expected operator-facing errors."""


class ValidationError(SubctlError):
    """Raised when config or registry input is invalid."""


class UpstreamError(SubctlError):
    """Raised when an upstream subscription cannot be fetched or decoded."""


class RenderError(SubctlError):
    """Raised when generated files cannot be rendered or written."""
