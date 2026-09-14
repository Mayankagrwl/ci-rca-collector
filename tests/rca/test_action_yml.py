"""Composite action contract (Slice C)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTION = (ROOT / "action.yml").read_text(encoding="utf-8")


def test_action_yml_has_host_overrides_empty_by_default() -> None:
    assert "github-api-url:" in ACTION
    assert "github-server-url:" in ACTION
    assert "default: ''" in ACTION


def test_action_yml_forwards_ssl_inputs() -> None:
    assert "ssl-verify:" in ACTION
    assert "ssl-cert-file:" in ACTION
    assert "RCA_SSL_VERIFY: ${{ inputs.ssl-verify }}" in ACTION
    assert "RCA_SSL_CERT_FILE: ${{ inputs.ssl-cert-file }}" in ACTION
    assert "SSL_CERT_FILE: ${{ env.SSL_CERT_FILE }}" in ACTION
    assert "REQUESTS_CA_BUNDLE: ${{ env.REQUESTS_CA_BUNDLE }}" in ACTION


def test_action_yml_forwards_host_and_token_env() -> None:
    assert "GITHUB_TOKEN: ${{ inputs.github-token }}" in ACTION
    assert "COMMON_ACTIONS_PAT: ${{ env.COMMON_ACTIONS_PAT }}" in ACTION
    assert "GITHUB_API_URL: ${{ inputs.github-api-url != '' && inputs.github-api-url || env.GITHUB_API_URL }}" in ACTION
    assert "GITHUB_SERVER_URL: ${{ inputs.github-server-url != '' && inputs.github-server-url || env.GITHUB_SERVER_URL }}" in ACTION
    assert "RCA_GITHUB_API_URL: ${{ inputs.github-api-url }}" in ACTION
    assert "RCA_GITHUB_HOST: ${{ inputs.github-server-url }}" in ACTION
    assert "PYTHONPATH: ${{ github.action_path }}" in ACTION
    assert "$GITHUB_ACTION_PATH" in ACTION


def test_action_yml_does_not_checkout_or_hardcode_hosts() -> None:
    assert "actions/checkout" not in ACTION
    assert "github.st.com" not in ACTION
    assert "api.github.com" not in ACTION


def test_run_steps_declare_bash() -> None:
    # Composite `run:` steps must set shell. `uses:` steps cannot.
    assert ACTION.count("shell: bash") >= 4
    assert "python --version" in ACTION
    assert "pip --version" in ACTION


def test_action_yml_analyze_is_separate_gated_step() -> None:
    assert "analyze:" in ACTION
    assert "stgpt-api-key:" in ACTION
    assert "stgpt-api-url:" in ACTION
    assert "default: 'true'" in ACTION
    assert "id: analyze" in ACTION
    assert "python -m tools.rca.cli analyze" in ACTION
    assert "steps.collect.outputs.requires-analysis == 'true'" in ACTION
    assert "inputs.stgpt-api-key != '' || env.STGPT_API != ''" in ACTION
    assert "STGPT_API: ${{ inputs.stgpt-api-key != '' && inputs.stgpt-api-key || env.STGPT_API }}" in ACTION
    assert "RCA_SSL_VERIFY: ${{ inputs.ssl-verify }}" in ACTION
    assert "root-cause:" in ACTION
    assert "suggested-fix:" in ACTION
    assert "rca-confidence:" in ACTION
    assert "analysis-status:" in ACTION
    assert "steps.analyze.outputs.root-cause" in ACTION
    assert "api-ai-bridge" not in ACTION
    collect_at = ACTION.index("id: collect")
    analyze_at = ACTION.index("id: analyze")
    summary_at = ACTION.index("Write job summary")
    assert collect_at < analyze_at < summary_at


def test_reusable_workflow_exists() -> None:
    text = (ROOT / ".github" / "workflows" / "rca.yml").read_text(encoding="utf-8")
    assert "workflow_call" in text
    assert ".drain/" in text
    assert ".rca-history/" in text
    assert "github.st.com" not in text
    assert "api.github.com" not in text
