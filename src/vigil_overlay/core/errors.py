"""Typed application errors used across service boundaries."""


class VigilOverlayError(Exception):
    """Base exception for expected application failures."""


class PathResolutionError(VigilOverlayError):
    """Raised when an application path cannot be resolved safely."""


class ConfigError(VigilOverlayError):
    """Raised for invalid or unsupported application configuration."""


class ManifestValidationError(VigilOverlayError):
    """Raised when a widget manifest violates the public contract."""


class ComponentValidationError(VigilOverlayError):
    """Raised when a declarative widget component tree is invalid."""


class ProtocolValidationError(VigilOverlayError):
    """Raised when a widget IPC message is malformed or unsafe."""


class UnsafeArchivePathError(VigilOverlayError):
    """Raised when an archive member could escape its installation root."""
