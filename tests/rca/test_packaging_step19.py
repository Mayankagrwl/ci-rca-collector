"""Step 19 — packaging: action.yml delivery modes, the write-scoped deliver job, examples,
the offline self-test. YAML is parsed with pyyaml; no GitHub needed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ACTION = yaml.safe_load((ROOT / "action.yml").read_text(encoding="utf-8"))
RCA = yaml.safe_load((ROOT / ".github" / "workflows" / "rca.yml").read_text(encoding="utf-8"))
PRE19 = json.loads((ROOT / "tests" / "rca" / "data" / "action-pre-step19.json").read_text(encoding="utf-8"))
STEPS = {step["name"]: step for step in ACTION["runs"]["steps"]}
EXAMPLES = ROOT / "docs" / "examples"
DELIVERY_OUTPUTS = ("severity", "suppressed-by", "delivered-to", "comment-url", "issue-url")
NEW_MODES = ("deliver", "feedback", "migrate-history")

SPEC_INPUTS = {  # delivery spec v1.3 §13 + Step 19
    "deliver": "false",
    "comment-on-pr": "true",
    "comment-on-commit": "true",
    "comment-on-branch-push": "false",
    "create-issues": "true",
    "issue-threshold": "3",
    "allow-fork-issues": "false",
    "confidence-threshold": "medium",
    "quiet-window-minutes": "60",
    "platform-team": "",
    "default-notify": "",
    "smtp-url": "",
    "chat-webhook-url": "",
    "chat-payload-field": "",
    "default-branch": "",
    "migrate-limit": "50",
}


def _on(doc: dict) -> Any:
    return doc.get("on", doc.get(True))  # pyyaml reads a bare `on:` key as True


def _runs(node: Any) -> list[str]:
    """Every `run:` body anywhere in a workflow / action document."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_runs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_runs(item))
    return found


def _eval_if(expr: str, mode: str) -> bool:
    """Tiny evaluator for the guard expressions: mode substituted, other inputs 'true'."""
    text = expr.replace("inputs.mode", repr(mode))
    text = re.sub(r"inputs\.[a-z-]+", "'true'", text)
    text = text.replace("&&", " and ").replace("||", " or ")
    return bool(eval(text, {"__builtins__": {}}, {}))  # noqa: S307 — a fixed, test-local grammar


# ---- AC1: action.yml -----------------------------------------------------------------------


def test_every_spec_input_with_its_default() -> None:
    for name, default in SPEC_INPUTS.items():
        assert name in ACTION["inputs"], name
        assert ACTION["inputs"][name].get("default") == default, name
    assert "secret" in ACTION["inputs"]["smtp-url"]["description"]
    assert "secret" in ACTION["inputs"]["chat-webhook-url"]["description"]
    assert "dry run" in ACTION["inputs"]["deliver"]["description"]
    assert "zero GitHub writes" in ACTION["inputs"]["deliver"]["description"]
    for mode in ("collect", "train", *NEW_MODES):
        assert mode in ACTION["inputs"]["mode"]["description"]


def test_delivery_outputs_reference_the_deliver_step() -> None:
    for name in DELIVERY_OUTPUTS:
        assert ACTION["outputs"][name]["value"] == f"${{{{ steps.deliver.outputs.{name} }}}}"


def test_collect_outputs_unchanged() -> None:
    for name, value in PRE19["outputs"].items():
        assert ACTION["outputs"][name]["value"] == value, name
    for name, default in PRE19["inputs"].items():
        if name != "run-id":  # now optional with '' (deliver / feedback / migrate don't need it)
            assert ACTION["inputs"][name].get("default") == default, name


def test_collect_train_steps_identical_except_guards() -> None:
    for old in PRE19["steps"]:
        new = STEPS[old["name"]]
        strip = lambda step: {k: v for k, v in step.items() if k != "if"}  # noqa: E731
        assert strip(new) == strip(old), old["name"]
        if "if" in old:  # an existing guard is kept verbatim
            assert new["if"] == old["if"], old["name"]


@pytest.mark.parametrize("mode", NEW_MODES)
def test_collect_train_steps_skip_in_new_modes(mode: str) -> None:
    for name in ("Compute cache keys", "Cache Drain3 baseline", "Cache fingerprint history",
                 "Run collector", "Report analysis", "Write job summary"):
        assert not _eval_if(STEPS[name]["if"], mode), (name, mode)
    for name in ("Set up Python", "Install collector dependencies"):
        assert "if" not in STEPS[name] or _eval_if(STEPS[name]["if"], mode)


@pytest.mark.parametrize("mode", ["collect", "train"])
def test_collect_train_still_run_their_steps(mode: str) -> None:
    for name in ("Compute cache keys", "Cache Drain3 baseline", "Cache fingerprint history", "Run collector"):
        assert _eval_if(STEPS[name]["if"], mode), (name, mode)
    for name in ("Deliver", "Feedback", "Migrate history"):
        assert not _eval_if(STEPS[name]["if"], mode)


@pytest.mark.parametrize(("step", "mode"), [("Deliver", "deliver"), ("Feedback", "feedback"),
                                           ("Migrate history", "migrate-history")])
def test_each_new_step_runs_only_in_its_mode(step: str, mode: str) -> None:
    for other in ("collect", "train", *NEW_MODES):
        assert _eval_if(STEPS[step]["if"], other) is (other == mode)


def test_history_backend_description_is_honest() -> None:
    desc = ACTION["inputs"]["history-backend"]["description"]
    assert "cache | none" in desc and "redis" not in desc and "issues |" not in desc
    assert "infra_widespread" in ACTION["outputs"]["short-circuit"]["description"]
    assert ACTION["outputs"]["diagnosis-source"]["description"] == "deterministic | ai | gated | none."


# ---- AC2: injection guard -----------------------------------------------------------------------


def _all_workflow_docs() -> dict[str, Any]:
    # Step 19b: every file in .github/workflows/ and docs/examples/, not a fixed list.
    docs = {"action.yml": ACTION}
    for path in [*(ROOT / ".github" / "workflows").glob("*.y*ml"), *EXAMPLES.glob("*.y*ml")]:
        docs[str(path.relative_to(ROOT))] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return docs


def test_no_expression_inside_any_run_body() -> None:
    docs = _all_workflow_docs()
    assert len(docs) >= 7 and ".github/workflows/eval.yml".replace("/", os.sep) in docs
    for name, doc in docs.items():
        for body in _runs(doc):
            assert "${{" not in body, f"{name}: {body[:80]}"


# ---- AC3: secrets ---------------------------------------------------------------------------------


def test_secrets_reach_the_script_only_as_masked_env() -> None:
    env = STEPS["Deliver"]["env"]
    assert env["RCA_SMTP_URL"] == "${{ inputs.smtp-url }}"
    assert env["RCA_CHAT_WEBHOOK_URL"] == "${{ inputs.chat-webhook-url }}"
    script = STEPS["Deliver"]["run"]
    lines = script.splitlines()
    assert lines[0] == 'if [ -n "$RCA_SMTP_URL" ]; then echo "::add-mask::$RCA_SMTP_URL"; fi'
    assert lines[1] == 'if [ -n "$RCA_CHAT_WEBHOOK_URL" ]; then echo "::add-mask::$RCA_CHAT_WEBHOOK_URL"; fi'
    for body in _runs(ACTION):
        rest = [ln for ln in body.splitlines() if "::add-mask::" not in ln]
        assert not any("SMTP_URL" in ln or "WEBHOOK_URL" in ln for ln in rest)
        assert "--smtp" not in body and "--chat" not in body


def test_deliver_env_resolves_host_like_collect() -> None:
    collect, deliver = STEPS["Run collector"]["env"], STEPS["Deliver"]["env"]
    for key in ("GITHUB_TOKEN", "GITHUB_API_URL", "GITHUB_SERVER_URL",
                "RCA_GITHUB_API_URL", "RCA_GITHUB_HOST", "PYTHONPATH", "RCA_SSL_VERIFY",
                "RCA_SSL_CERT_FILE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        assert deliver[key] == collect[key], key
    assert deliver["RCA_DEFAULT_BRANCH"] == (
        "${{ inputs.default-branch != '' && inputs.default-branch || github.event.repository.default_branch }}"
    )


# ---- AC4: the deliver argv harness -------------------------------------------------------------------


BASH = shutil.which("bash")
_HARNESS_ENV = {
    "RCA_OUT": "rca", "RCA_REPO": "acme/widgets", "RCA_STRICT": "false", "RCA_WRITE_SUMMARY": "true",
    "RCA_COMMENT_ON_PR": "true", "RCA_COMMENT_ON_COMMIT": "false", "RCA_COMMENT_ON_BRANCH_PUSH": "true",
    "RCA_CREATE_ISSUES": "false", "RCA_ISSUE_THRESHOLD": "5", "RCA_ALLOW_FORK_ISSUES": "true",
    "RCA_CONFIDENCE_THRESHOLD": "high", "RCA_QUIET_WINDOW_MINUTES": "30",
    "RCA_PLATFORM_TEAM": "@org/platform", "RCA_DEFAULT_NOTIFY": "oncall@example.invalid",
    "RCA_SMTP_URL": "smtp://u:secretpw@smtp.example.invalid?from=a@example.invalid",
    "RCA_CHAT_WEBHOOK_URL": "https://hooks.example.invalid/SECRETPATH",
}


def run_harness(tmp_path: Path, step: str, **overrides: str) -> tuple[list[str], str]:
    """Source a step's script with `python` stubbed to record its argv."""
    argv_file = tmp_path / "argv.txt"
    script = tmp_path / "step.sh"
    script.write_text(
        "set -eo pipefail\n"
        'python() { printf \'%s\\n\' "$@" > "$ARGV_OUT"; }\n'
        + STEPS[step]["run"],
        encoding="utf-8",
        newline="\n",
    )
    env = {**os.environ, **_HARNESS_ENV, **overrides}
    env.update(ARGV_OUT=str(argv_file).replace("\\", "/"),
               GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md").replace("\\", "/"),
               GITHUB_EVENT_PATH="/tmp/event.json")
    out = subprocess.run([BASH, str(script).replace("\\", "/")], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=True)
    return argv_file.read_text(encoding="utf-8").splitlines(), out.stdout


pytestmark_bash = pytest.mark.skipif(BASH is None, reason="bash not available")


@pytestmark_bash
@pytest.mark.parametrize(("value", "live"), [("true", True), ("false", False), ("", False), ("TRUE", False)])
def test_deliver_flag_maps_to_live_or_dry_run(tmp_path, value, live) -> None:
    argv, stdout = run_harness(tmp_path, "Deliver", RCA_DELIVER=value)
    assert argv[:3] == ["-m", "tools.rca.cli", "deliver"]
    assert ("--live" in argv) is live and ("--dry-run" in argv) is (not live)
    assert "::add-mask::" + _HARNESS_ENV["RCA_SMTP_URL"] in stdout
    assert "::add-mask::" + _HARNESS_ENV["RCA_CHAT_WEBHOOK_URL"] in stdout
    joined = "\n".join(argv)
    assert "secretpw" not in joined and "SECRETPATH" not in joined


@pytestmark_bash
def test_every_input_maps_to_its_flag(tmp_path) -> None:
    argv, _ = run_harness(tmp_path, "Deliver", RCA_DELIVER="true")
    pairs = dict(zip(argv[::1], argv[1::1]))
    expected = {
        "--summary": "rca/summary.json", "--out": "rca", "--repo": "acme/widgets",
        "--comment-on-pr": "true", "--comment-on-commit": "false", "--comment-on-branch-push": "true",
        "--create-issues": "false", "--issue-threshold": "5", "--allow-fork-issues": "true",
        "--confidence-threshold": "high", "--quiet-window-minutes": "30",
        "--platform-team": "@org/platform", "--default-notify": "oncall@example.invalid",
    }
    for flag, value in expected.items():
        assert pairs.get(flag) == value, flag
    assert "--strict" not in argv


@pytestmark_bash
def test_empty_values_are_omitted_and_strict_maps(tmp_path) -> None:
    argv, _ = run_harness(tmp_path, "Deliver", RCA_DELIVER="false", RCA_PLATFORM_TEAM="",
                          RCA_DEFAULT_NOTIFY="", RCA_STRICT="true", RCA_SMTP_URL="", RCA_CHAT_WEBHOOK_URL="")
    assert "--platform-team" not in argv and "--default-notify" not in argv
    assert "--strict" in argv


@pytestmark_bash
def test_feedback_and_migrate_argv(tmp_path) -> None:
    argv, _ = run_harness(tmp_path, "Feedback", RCA_DELIVER="true")
    assert argv[:5] == ["-m", "tools.rca.cli", "feedback", "--event", "/tmp/event.json"]
    assert "--live" in argv
    argv, _ = run_harness(tmp_path, "Migrate history", RCA_DELIVER="false",
                          RCA_HISTORY_DIR=".rca-history", RCA_MIGRATE_LIMIT="7")
    assert argv[2:8] == ["deliver", "--migrate-history", "--history-dir", ".rca-history",
                         "--migrate-limit", "7"]
    assert "--dry-run" in argv and "--live" not in argv


# ---- AC5: rca.yml ---------------------------------------------------------------------------------------


def test_rca_permissions_split() -> None:
    assert RCA["permissions"] == {"actions": "read", "contents": "read"}
    assert RCA["jobs"]["collect"]["permissions"] == {"actions": "read", "contents": "read"}
    assert RCA["jobs"]["deliver"]["permissions"] == {
        "actions": "read", "contents": "write", "pull-requests": "write", "issues": "write"}


def test_rca_deliver_job_shape() -> None:
    job = RCA["jobs"]["deliver"]
    assert job["needs"] == "collect"
    assert job["if"] == "inputs.conclusion == 'failure' && needs.collect.result == 'success'"
    assert job["timeout-minutes"] == 5
    download, deliver = job["steps"]
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {"name": "rca-${{ inputs.run-id }}", "path": "rca/"}
    assert deliver["uses"] == RCA["jobs"]["collect"]["steps"][3]["uses"]  # same pinned ref
    assert deliver["with"]["mode"] == "deliver"
    assert deliver["with"]["smtp-url"] == "${{ secrets.smtp-url }}"
    assert deliver["with"]["chat-webhook-url"] == "${{ secrets.chat-webhook-url }}"
    assert not any("checkout" in str(step.get("uses", "")) for step in job["steps"])
    for name in DELIVERY_OUTPUTS:
        assert job["outputs"][name] == f"${{{{ steps.deliver.outputs.{name} }}}}"


def test_rca_inputs_secrets_outputs() -> None:
    call = _on(RCA)["workflow_call"]
    assert call["inputs"]["default-branch"]["default"] == ""
    assert call["inputs"]["deliver"] == {"required": False, "type": "boolean", "default": False}
    for name, default in SPEC_INPUTS.items():
        if name in {"deliver", "smtp-url", "chat-webhook-url", "default-branch", "migrate-limit"}:
            continue
        assert call["inputs"][name]["default"] == default, name
    assert call["secrets"] == {"smtp-url": {"required": False}, "chat-webhook-url": {"required": False}}
    for name in DELIVERY_OUTPUTS:
        assert call["outputs"][name]["value"] == f"${{{{ jobs.deliver.outputs.{name} }}}}"
    upload = RCA["jobs"]["collect"]["steps"][-1]
    assert upload["if"] == "inputs.conclusion == 'failure'"
    text = (ROOT / ".github" / "workflows" / "rca.yml").read_text(encoding="utf-8")
    assert "can only NARROW the caller's token" in text


# ---- AC6: examples --------------------------------------------------------------------------------------


def test_consumer_example() -> None:
    doc = yaml.safe_load((EXAMPLES / "rca-consumer.yml").read_text(encoding="utf-8"))
    assert _on(doc) == {"workflow_run": {"workflows": ["CI"], "types": ["completed"]}}
    job = doc["jobs"]["rca"]
    assert job["uses"].endswith("/.github/workflows/rca.yml@v1")
    assert job["with"]["deliver"] is False
    assert job["with"]["conclusion"] == "${{ github.event.workflow_run.conclusion }}"
    assert set(job["secrets"]) == {"smtp-url", "chat-webhook-url"}
    assert doc["permissions"] == {"actions": "read", "contents": "write", "pull-requests": "write",
                                  "issues": "write"}


def test_feedback_example_uses_the_action_and_checks_nothing_out() -> None:
    raw = (EXAMPLES / "rca-feedback.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    (step,) = doc["jobs"]["resolved"]["steps"]
    assert step["uses"] == "acme/ci-rca-collector@v1"
    assert step["with"] == {"mode": "feedback", "deliver": "true"}
    assert "actions/checkout" not in raw
    assert doc["jobs"]["resolved"]["if"].startswith("startsWith(github.event.comment.body, '/resolved')")


def test_migrate_example_reuses_the_history_key_scheme() -> None:
    rca_raw = (ROOT / ".github" / "workflows" / "rca.yml").read_text(encoding="utf-8")
    raw = (EXAMPLES / "rca-migrate.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    pattern = re.compile(r"          sanitize\(\) \{.*?\n          \}\n", re.S)
    assert pattern.search(rca_raw).group(0) == pattern.search(raw).group(0)
    for line in ('echo "history-cache-key=rca-history-${repo}-${wf}"',
                 'echo "history-cache-restore-key=rca-history-${repo}-"'):
        assert line in rca_raw and line in raw
    assert doc["permissions"] == {"issues": "write"}
    assert set(_on(doc)["workflow_dispatch"]["inputs"]) == {"workflow-name", "live"}
    steps = {step["name"]: step for step in doc["jobs"]["migrate"]["steps"]}
    migrate = steps["Migrate"]
    assert migrate["with"]["mode"] == "migrate-history"
    restore = steps["Restore fingerprint history"]
    assert restore["with"]["path"] == ".rca-history/"


# ---- AC7: the offline self-test ------------------------------------------------------------------------------


def test_self_test_job_shape() -> None:
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "self-test.yml").read_text(encoding="utf-8"))
    job = doc["jobs"]["delivery-dry-run"]
    assert job["permissions"] == {"contents": "read"}
    body = job["steps"][-1]["run"]
    assert "--dry-run --offline" in body and "test -f rca-selftest/delivery-preview.md" in body
    assert "grep -q '^severity=' \"$GITHUB_OUTPUT\"" in body


def test_self_test_command_runs_offline(tmp_path) -> None:
    out_file = tmp_path / "gh_output"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RCA_", "GITHUB_"))}
    env.update(PYTHONPATH=str(ROOT), GITHUB_OUTPUT=str(out_file))
    golden = ROOT / "tools" / "eval" / "goldens" / "artifactory-version-exists" / "summary.json"
    before = golden.read_bytes()
    subprocess.run([sys.executable, "-m", "tools.rca.cli", "deliver", "--summary", str(golden),
                    "--out", str(tmp_path / "rca-selftest"), "--dry-run", "--offline"],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    assert (tmp_path / "rca-selftest" / "delivery-preview.md").exists()
    assert re.search(r"(?m)^severity=\w+$", out_file.read_text(encoding="utf-8"))
    assert golden.read_bytes() == before


# ---- docs ------------------------------------------------------------------------------------------------------


def test_agents_and_readme_sections() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "## Phase 3 packaging" in agents and "dry run" in agents
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "## Delivery (Phase 3)" in readme and "### Deferred" in readme
    for name in ("rca-consumer.yml", "rca-feedback.yml", "rca-migrate.yml"):
        assert name in readme
    assert "Inferred resolution" in readme and "concurrent_" in readme


# ---- Step 19b ----------------------------------------------------------------------------------------------


WRITE_MODE_STEPS = ("Deliver", "Feedback", "Migrate history")


@pytest.mark.parametrize("name", WRITE_MODE_STEPS)
def test_write_modes_use_the_job_token_never_the_org_pat(name: str) -> None:  # fix 1
    step = STEPS[name]
    assert step["env"]["RCA_GITHUB_TOKEN"] == "${{ inputs.github-token }}"
    assert "COMMON_ACTIONS_PAT" not in step["env"]
    assert "COMMON_ACTIONS_PAT" not in json.dumps(step)
    assert "job's permissions" in " ".join(ACTION["inputs"]["github-token"]["description"].split())


def test_collect_still_passes_the_pat_unchanged() -> None:  # fix 1: collect untouched
    old = next(step for step in PRE19["steps"] if step["name"] == "Run collector")
    assert STEPS["Run collector"]["env"] == old["env"]
    assert "RCA_GITHUB_TOKEN" not in STEPS["Run collector"]["env"]


def test_migrate_example_restores_exact_key_only() -> None:  # fix 2
    raw = (EXAMPLES / "rca-migrate.yml").read_text(encoding="utf-8")
    doc = yaml.safe_load(raw)
    steps = {step["name"]: step for step in doc["jobs"]["migrate"]["steps"]}
    restore = steps["Restore fingerprint history"]
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["id"] == "restore"
    assert restore["with"]["key"] == "${{ steps.keys.outputs.history-cache-key }}"
    assert "restore-keys" not in restore["with"]
    assert "actions/cache@v4" not in [step.get("uses") for step in doc["jobs"]["migrate"]["steps"]]
    assert steps["Migrate"]["if"] == "steps.restore.outputs.cache-hit == 'true'"
    miss = steps["No history cache"]
    assert miss["if"] == "steps.restore.outputs.cache-hit != 'true'"
    assert miss["env"] == {"WF": "${{ inputs.workflow-name }}"}
    assert "nothing to migrate" in miss["run"] and "$WF" in miss["run"]
    assert "without actions/cache/restore" in raw  # the GHES note


def test_rca_upload_overwrites_on_rerun() -> None:  # fix 3
    upload = RCA["jobs"]["collect"]["steps"][-1]
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["overwrite"] is True


def test_eval_runs_input_via_env_and_validated() -> None:  # fix 5
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "eval.yml").read_text(encoding="utf-8"))
    step = next(st for job in doc["jobs"].values() for st in job["steps"] if st.get("name") == "Run eval")
    assert step["env"]["RCA_EVAL_RUNS"] == "${{ github.event.inputs.runs || '3' }}"
    assert '[[ "$RCA_EVAL_RUNS" =~ ^[0-9]+$ ]]' in step["run"]
    assert "${{" not in step["run"]


@pytestmark_bash
@pytest.mark.parametrize(("value", "runs"), [("5", "5"), ("", "3"), ("0", "3"), ("3; rm -rf /", "3"), ("abc", "3")])
def test_eval_runs_validation(tmp_path, value, runs) -> None:  # fix 5, executed
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "eval.yml").read_text(encoding="utf-8"))
    body = next(st for job in doc["jobs"].values() for st in job["steps"] if st.get("name") == "Run eval")["run"]
    script = tmp_path / "eval.sh"
    script.write_text(
        'python() { :; }\n' + body.replace('python -m tools.eval.run_eval', 'echo "RUNS=$RUNS"; python'),
        encoding="utf-8", newline="\n",
    )
    env = {**os.environ, "RCA_EVAL_RUNS": value, "STGPT_API": "set"}
    out = subprocess.run([BASH, str(script).replace("\\", "/")], env=env, capture_output=True, text=True, check=True)
    assert f"RUNS={runs}" in out.stdout
