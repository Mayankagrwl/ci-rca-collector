"""Golden label schema + loader (eval-spec §7.1).

A golden dir holds ``summary.json`` (a redacted, captured collector Summary) and
``label.yaml`` (ground truth). A malformed ``label.yaml`` fails loudly and names
the file (§11.14) — goldens are never skipped silently.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator

from tools.rca.config import CLASSIFY_RULES
from tools.rca.models import Summary

# Stage-5 categories the labels are validated against: the CLASSIFY_RULES
# categories plus the short-circuit / residual categories a verdict can carry.
VALID_CATEGORIES: frozenset[str] = frozenset(
    {rule["category"] for rule in CLASSIFY_RULES}
    | {"infra_runner", "infra_widespread", "unknown"}
)


class GoldenLabelError(ValueError):
    """Raised when a label.yaml is missing or malformed; names the file."""


class GoldenLabel(BaseModel):
    schema_version: str = "1.0"
    slug: str
    source: Literal["real", "synthetic"]
    captured_from_run: int | None = None
    captured_at: date
    true_category: str
    is_infra_vs_code: Literal["infra", "code"]
    is_flaky: bool = False
    expect_short_circuit: (
        Literal["infra_runner", "infra_widespread", "flake_same_sha_passed"] | None
    ) = None
    # Judgement-only fields — recorded, unused by 11a metrics.
    root_cause: str | None = None
    acceptable_fixes: list[str] = []
    labelled_by: str | None = None
    labelled_at: date | None = None
    notes: str | None = None

    @field_validator("true_category")
    @classmethod
    def _category_is_known(cls, value: str) -> str:
        if value not in VALID_CATEGORIES:
            raise ValueError(
                f"true_category {value!r} is not a Stage-5 category "
                f"({sorted(VALID_CATEGORIES)})"
            )
        return value


def load_label(path: Path) -> GoldenLabel:
    """Load one label.yaml. Raises GoldenLabelError naming the file on any error."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise GoldenLabelError(f"cannot read label {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GoldenLabelError(f"label {path} is not a mapping")
    try:
        return GoldenLabel.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — re-raise with the file name attached
        raise GoldenLabelError(f"invalid label {path}: {exc}") from exc


def load_summary(path: Path) -> Summary:
    path = Path(path)
    try:
        return Summary.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise GoldenLabelError(f"invalid summary {path}: {exc}") from exc


def load_goldens(
    directory: Path,
) -> list[tuple[str, Summary, GoldenLabel, Path]]:
    """Load every golden under *directory* (each dir with label.yaml + summary.json)."""
    directory = Path(directory)
    out: list[tuple[str, Summary, GoldenLabel, Path]] = []
    for golden_dir in sorted(p for p in directory.iterdir() if p.is_dir()):
        label_path = golden_dir / "label.yaml"
        summary_path = golden_dir / "summary.json"
        if not label_path.exists():
            continue
        if not summary_path.exists():
            raise GoldenLabelError(f"golden {golden_dir} has label but no summary.json")
        label = load_label(label_path)
        summary = load_summary(summary_path)
        out.append((label.slug or golden_dir.name, summary, label, golden_dir))
    return out
