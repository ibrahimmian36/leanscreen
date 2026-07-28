"""Typed settings, populated from ``LEANSCREEN_``-prefixed env vars (or `.env`).

Only what the screen needs: where Lean lives, which models judge, and the
HTTP budget. No database, no storage, no pipeline knobs.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings (env prefix ``LEANSCREEN_``)."""

    model_config = SettingsConfigDict(
        env_prefix="LEANSCREEN_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Lean toolchain -----------------------------------------------------
    lean_project_path: Path | None = Field(
        default=None,
        description="A Lean 4 project with mathlib as a dependency (built with `lake build`).",
    )
    lean_repl_path: Path | None = Field(
        default=None,
        description=(
            "Path to the community REPL binary (repl/.lake/build/bin/repl). "
            "With it, mathlib loads once (~100s) and each check takes ~0.1s; "
            "without it, every check pays a full `lake env lean` invocation."
        ),
    )
    lean_timeout_seconds: float = Field(default=180.0, gt=0.0)

    # --- Anthropic API (check_deep only) ------------------------------------
    anthropic_base_url: str = Field(default="https://api.anthropic.com/v1/messages")
    anthropic_version: str = Field(default="2023-06-01")
    anthropic_model: str = Field(
        default="claude-opus-4-8",
        description="Model for judge A and the counterexample probe (the calibrated default).",
    )
    judge_b_model: str = Field(
        default="claude-fable-5",
        description=(
            "Model for the checklist judge (judge B). The default matches the "
            "calibrated configuration. Fable/Mythos are locked-surface models: "
            "thinking is always on and billed as output tokens, so judge B "
            "gets a 32k max_tokens budget automatically."
        ),
    )
    anthropic_timeout_seconds: float = Field(
        default=600.0,
        gt=0.0,
        description=(
            "HTTP read timeout. Always-thinking models can spend minutes on one "
            "response, and a timeout that fires mid-generation still bills the "
            "full response server-side — keep this generous."
        ),
    )
    max_tokens: int = Field(
        default=4096,
        ge=1,
        description="Response budget for judge A (judge B is sized automatically).",
    )
