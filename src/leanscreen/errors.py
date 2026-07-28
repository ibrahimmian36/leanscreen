"""Typed error hierarchy.

Ground rule: no bare excepts, no silent failures. Every failure path raises a
typed error from this module so callers can branch on *why* something failed.
"""

from __future__ import annotations


class LeanScreenError(Exception):
    """Base class for every error raised by this package."""

    #: Stable machine-readable code.
    code: str = "leanscreen_error"


class ConfigError(LeanScreenError):
    """Invalid or missing configuration."""

    code = "config_error"


class LLMError(LeanScreenError):
    """The language-model API failed or returned an unusable response."""

    code = "llm_error"


class VerifierError(LeanScreenError):
    """The Lean verifier could not be invoked (toolchain/config problem).

    Note: a Lean *rejection* of a statement is a normal result, not this error;
    this is raised only when the verifier itself cannot run.
    """

    code = "verifier_error"


class CreditsExhaustedError(LeanScreenError):
    """The API account has no credit — a GLOBAL condition, not a per-call one.

    Deliberately NOT an LLMError subclass: per-call handlers catch LLMError
    and continue, but an empty account dooms every subsequent call, so this
    must propagate instead of being absorbed.
    """

    code = "credits_exhausted"
