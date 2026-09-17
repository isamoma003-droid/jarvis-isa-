"""Exception types shared across Jarvis."""

from __future__ import annotations


class JarvisError(Exception):
    """Base class for every error Jarvis raises deliberately."""


class ConfigError(JarvisError):
    """The configuration is missing something or holds an impossible value."""


class ToolError(JarvisError):
    """A tool failed in a way the model should see and can recover from."""


class ApprovalDenied(ToolError):
    """A tool needed human approval and did not get it."""


class SandboxViolation(ToolError):
    """A path or command tried to leave the workspace."""


class MissingDependency(JarvisError):
    """An optional extra (voice, web) is not installed."""

    def __init__(self, feature: str, extra: str, package: str) -> None:
        super().__init__(
            f"{feature} needs the '{package}' package. "
            f"Install it with: pip install 'jarvis[{extra}]'"
        )
        self.feature = feature
        self.extra = extra
        self.package = package
