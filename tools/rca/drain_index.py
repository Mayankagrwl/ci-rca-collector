"""Drain3 wrapper: train on healthy logs, novelty on failures, fingerprints.

Source-agnostic. Persistence key is an opaque string (workflow + job name).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import statistics
from collections import defaultdict
from configparser import ConfigParser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from drain3 import TemplateMiner
from drain3.file_persistence import FilePersistence
from drain3.persistence_handler import PersistenceHandler
from drain3.template_miner_config import TemplateMinerConfig

from .extract import ERROR_LINE
from .models import DrainReport, LogTemplate, TemplateVariable
from .redact import redact_text

_LOG = logging.getLogger(__name__)
_TIER_ORDER = {"T1": 0, "T2": 1, "T3": 2, "T4": 3, "T5": 4}


class _LoadOnlyPersistence(PersistenceHandler):
    """Load a snapshot without writing failing lines back into the baseline."""

    def __init__(self, path: str) -> None:
        self._inner = FilePersistence(path)

    def save_state(self, state: bytes) -> None:  # noqa: ARG002
        return None

    def load_state(self) -> bytes | None:
        return self._inner.load_state()


def default_config_path() -> Path:
    env = None
    try:
        import os

        env = os.environ.get("RCA_DRAIN_CONFIG")
    except Exception:  # pragma: no cover
        env = None
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "drain3.ini"


def masking_config_hash(ini_path: str) -> str:
    """sha256 over the normalised [MASKING] and [DRAIN] sections, first 12 hex."""
    parser = ConfigParser()
    read = parser.read(ini_path)
    if not read:
        raise FileNotFoundError(ini_path)
    raw = parser.get("MASKING", "masking", fallback="[]")
    instructions = json.loads(raw)
    normalised = sorted(
        (
            {
                "regex_pattern": item.get("regex_pattern", ""),
                "mask_with": item.get("mask_with", ""),
            }
            for item in instructions
        ),
        key=lambda item: (item["regex_pattern"], item["mask_with"]),
    )
    payload = {
        "depth": parser.get("DRAIN", "depth", fallback="").strip(),
        "mask_prefix": parser.get("MASKING", "mask_prefix", fallback="").strip(),
        "mask_suffix": parser.get("MASKING", "mask_suffix", fallback="").strip(),
        "masking": normalised,
        "max_children": parser.get("DRAIN", "max_children", fallback="").strip(),
        "sim_th": parser.get("DRAIN", "sim_th", fallback="").strip(),
    }
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def template_hash(template: str) -> str:
    """Per-template set element for Jaccard. Not a fingerprint."""
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """Jaccard over template strings (or their per-template hashes). Never cluster IDs."""
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def fingerprint_fine(templates: Sequence[str]) -> str:
    joined = "\n".join(sorted(templates))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def fingerprint_coarse(template: str | None) -> str:
    if not template:
        return hashlib.sha256(b"").hexdigest()[:16]
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]


def persistence_filename(key: str) -> str:
    safe = re.sub(r"[^\w.-]+", "_", key).strip("_") or "default"
    return f"{safe}.bin"


def workflow_bin_prefix(workflow: str) -> str:
    prefix = re.sub(r"[^\w.-]+", "_", f"{workflow}_")
    if not prefix.endswith("_"):
        prefix += "_"
    return prefix


def resolve_state_path(
    key: str,
    drain_dir: str | Path,
    *,
    workflow: str | None = None,
) -> tuple[Path, str | None]:
    """Exact `{workflow}_{job}.bin`, else any `{workflow}_*.bin` fallback."""
    directory = Path(drain_dir)
    exact = directory / persistence_filename(key)
    if exact.exists():
        return exact, None
    if not workflow or not directory.is_dir():
        return exact, None
    prefix = workflow_bin_prefix(workflow)
    matches = sorted(path for path in directory.glob("*.bin") if path.name.startswith(prefix))
    if not matches:
        return exact, None
    chosen = matches[0]
    return chosen, chosen.name


@dataclass
class NoveltyResult:
    report: DrainReport
    fingerprint_fine: str
    fingerprint_coarse: str
    hash_input: list[str] = field(default_factory=list)
    fallback_file: str | None = None


def _load_config(config_path: str | Path) -> TemplateMinerConfig:
    cfg = TemplateMinerConfig()
    cfg.load(str(config_path))
    return cfg


def _miner(
    *,
    state_path: Path,
    config_path: str | Path,
    persist: bool,
) -> TemplateMiner:
    cfg = _load_config(config_path)
    if persist:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        handler: PersistenceHandler = FilePersistence(str(state_path))
    elif state_path.exists():
        handler = _LoadOnlyPersistence(str(state_path))
    else:
        handler = None  # type: ignore[assignment]
    return TemplateMiner(handler, cfg)


def train(
    lines: Sequence[str],
    key: str,
    *,
    drain_dir: str | Path,
    config_path: str | Path | None = None,
) -> Path:
    ini = str(config_path or default_config_path())
    path = Path(drain_dir) / persistence_filename(key)
    miner = _miner(state_path=path, config_path=ini, persist=True)
    for line in lines:
        if line.strip():
            miner.add_log_message(line)
    miner.save_state("train")
    resolved = path.resolve()
    _LOG.info("Drain3 wrote %s", resolved)
    return resolved


def novelty(
    lines: Sequence[str],
    key: str,
    *,
    drain_dir: str | Path,
    config_path: str | Path | None = None,
    workflow: str | None = None,
) -> NoveltyResult:
    ini = str(config_path or default_config_path())
    path, fallback_file = resolve_state_path(key, drain_dir, workflow=workflow)
    available = path.exists()
    if fallback_file:
        _LOG.info("Drain3 fallback %s (job key %s missing)", path, key)
    miner = _miner(state_path=path, config_path=ini, persist=False)
    baseline_counts: dict[str, int] = {}
    if available:
        baseline_counts = {
            cluster.get_template(): int(cluster.size) for cluster in miner.drain.clusters
        }
    tallies: dict[str, dict[str, Any]] = {}
    for index, line in enumerate(lines, start=1):
        if not str(line).strip():
            continue
        result = miner.add_log_message(line)
        template = str(result.get("template_mined") or "")
        bucket = tallies.setdefault(
            template,
            {
                "count": 0,
                "first_line": index,
                "representative": line,
                "created": False,
                "error": False,
                "params": [],
            },
        )
        bucket["count"] += 1
        if result.get("change_type") == "cluster_created":
            bucket["created"] = True
        if ERROR_LINE.search(line):
            bucket["error"] = True
        extracted = miner.extract_parameters(template, line, exact_matching=False) or []
        bucket["params"].append(extracted)

    templates = _build_templates(tallies, baseline_counts, available)
    templates = _apply_anomalies(templates, available)
    templates.sort(key=lambda item: (_TIER_ORDER[item.tier], item.first_line, item.template))
    for ordinal, item in enumerate(templates, start=1):
        item.template_id = ordinal

    hash_input = _fingerprint_inputs(templates, available)
    highest = next((t.template for t in templates if t.tier == "T1"), None)
    if highest is None and not available:
        highest = next((t.template for t in templates if t.tier == "T3"), None)
    if highest is None and templates:
        highest = templates[0].template
    fine = fingerprint_fine(hash_input)
    coarse = fingerprint_coarse(highest)
    degraded = not available
    tier_counts: dict[str, int] = {tier: 0 for tier in _TIER_ORDER}
    for item in templates:
        tier_counts[item.tier] = tier_counts.get(item.tier, 0) + 1
    report = DrainReport(
        baseline_available=available,
        baseline_template_count=len(baseline_counts) if available else None,
        total_clusters=len(templates),
        tier_counts=tier_counts,
        templates=templates,
        fingerprint_degraded=degraded,
        masking_config_hash=masking_config_hash(ini),
    )
    return NoveltyResult(
        report=report,
        fingerprint_fine=fine,
        fingerprint_coarse=coarse,
        hash_input=hash_input,
        fallback_file=fallback_file,
    )


def _fingerprint_inputs(templates: Sequence[LogTemplate], available: bool) -> list[str]:
    if available:
        chosen = [t.template for t in templates if t.tier in {"T1", "T2"}]
    else:
        chosen = [t.template for t in templates if t.tier == "T3"]
        if not chosen:
            chosen = [t.template for t in templates if t.has_error_match]
    return chosen


def _build_templates(
    tallies: dict[str, dict[str, Any]],
    baseline_counts: dict[str, int],
    available: bool,
) -> list[LogTemplate]:
    templates: list[LogTemplate] = []
    seen = set(tallies)
    for template, bucket in tallies.items():
        count = int(bucket["count"])
        if available:
            is_novel = template not in baseline_counts
            baseline = int(baseline_counts.get(template, 0))
        else:
            is_novel = None
            baseline = None
        has_error = bool(bucket["error"])
        tier = _tier(is_novel, has_error, count, available)
        variables = _variables(bucket["params"])
        representative, _ = redact_text(str(bucket["representative"]))
        safe_template, _ = redact_text(template)
        templates.append(
            LogTemplate(
                template_id=0,
                template=safe_template,
                count=count,
                baseline_count=baseline,
                is_novel=is_novel,
                first_line=int(bucket["first_line"]),
                has_error_match=has_error,
                tier=tier,
                variables=variables,
                representative_line=representative,
            )
        )
    if available:
        for template, base_count in baseline_counts.items():
            if template in seen:
                continue
            is_novel = False
            has_error = False
            count = 0
            tier = _tier(is_novel, has_error, count, True)
            safe_template, _ = redact_text(template)
            templates.append(
                LogTemplate(
                    template_id=0,
                    template=safe_template,
                    count=count,
                    baseline_count=base_count,
                    is_novel=False,
                    first_line=0,
                    has_error_match=False,
                    tier=tier,
                    variables=[],
                    representative_line=None,
                )
            )
    return templates


def _tier(
    is_novel: bool | None,
    has_error: bool,
    count: int,
    available: bool,
) -> str:
    if not available:
        if has_error and count <= 5:
            return "T3"
        if has_error:
            return "T4"
        return "T5"
    if is_novel and has_error:
        return "T1"
    if is_novel:
        return "T2"
    if has_error and count <= 5:
        return "T3"
    if has_error:
        return "T4"
    return "T5"


def _apply_anomalies(templates: list[LogTemplate], available: bool) -> list[LogTemplate]:
    if not available:
        return templates
    for item in templates:
        base = item.baseline_count
        if base is None or base < 20:
            continue
        if item.count == 0:
            item.anomaly = "missing"
            item.anomaly_ratio = 0.0
            item.tier = "T2"
        elif item.count < base * 0.25:
            item.anomaly = "depleted"
            item.anomaly_ratio = item.count / base
            item.tier = "T2"
        elif item.count > base * 10 and item.count >= 50:
            item.anomaly = "flooding"
            item.anomaly_ratio = item.count / base
            item.tier = "T2"
    return templates


def _variables(occurrences: list[Any]) -> list[TemplateVariable]:
    if not occurrences:
        return []
    columns: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for extracted in occurrences:
        for position, param in enumerate(extracted or []):
            value = getattr(param, "value", str(param))
            mask = getattr(param, "mask_name", "*")
            redacted, _ = redact_text(str(value))
            columns[position].append((redacted, str(mask)))
    out: list[TemplateVariable] = []
    for position in sorted(columns):
        pairs = columns[position]
        values = [v for v, _m in pairs]
        mask = pairs[0][1] if pairs else "*"
        numeric = _as_floats(values)
        if numeric is not None and len(numeric) >= 1:
            kind: str = "numeric"
            extra: dict[str, Any] = {
                "minimum": min(numeric),
                "maximum": max(numeric),
                "median": statistics.median(numeric),
            }
            if _looks_like_sequence(numeric):
                kind = "sequence"
                extra["sequence_rle"] = _rle(numeric)
            out.append(
                TemplateVariable(
                    position=position,
                    mask=mask,
                    kind=kind,  # type: ignore[arg-type]
                    values=[],
                    **extra,
                )
            )
            continue
        distinct = list(dict.fromkeys(values))
        if len(distinct) <= 5:
            out.append(
                TemplateVariable(
                    position=position,
                    mask=mask,
                    kind="string_low",
                    values=distinct,
                    distinct_count=len(distinct),
                )
            )
        else:
            out.append(
                TemplateVariable(
                    position=position,
                    mask=mask,
                    kind="string_high",
                    values=distinct[:3],
                    distinct_count=len(distinct),
                )
            )
    return out


def _as_floats(values: Sequence[str]) -> list[float] | None:
    parsed: list[float] = []
    for value in values:
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            return None
    return parsed


def _looks_like_sequence(values: Sequence[float]) -> bool:
    if len(values) < 4:
        return False
    return all(values[i] <= values[i + 1] for i in range(len(values) - 1))


def _rle(values: Sequence[float]) -> str:
    parts: list[str] = []
    i = 0
    while i < len(values):
        j = i + 1
        while j < len(values) and values[j] == values[i]:
            j += 1
        n = j - i
        token = _fmt_num(values[i])
        if n >= 3:
            parts.append(f"{token}×{n}")
        else:
            parts.extend([token] * n)
        i = j
    return "[" + ", ".join(parts) + "]"


def _fmt_num(value: float) -> str:
    if math.isfinite(value) and value == int(value):
        return str(int(value))
    return str(value)


def remask_templates(
    templates: Sequence[str],
    *,
    config_path: str | Path | None = None,
) -> list[str]:
    """Replay stored template strings through the current masking config (§10.3)."""
    ini = str(config_path or default_config_path())
    miner = TemplateMiner(None, _load_config(ini))
    return [miner.masker.mask(item) for item in templates]
