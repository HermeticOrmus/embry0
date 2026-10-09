"""Embry0Config new defaults for execution_mode / auth_mode."""

from embry0.config import Embry0Config


def test_default_execution_mode_is_sdk() -> None:
    cfg = Embry0Config()
    assert cfg.default_execution_mode == "sdk"


def test_default_auth_mode_is_oauth() -> None:
    cfg = Embry0Config()
    assert cfg.default_auth_mode == "oauth"


def test_defaults_overridable_via_env(monkeypatch) -> None:
    monkeypatch.setenv("DEFAULT_EXECUTION_MODE", "cli")
    monkeypatch.setenv("DEFAULT_AUTH_MODE", "api_key")
    cfg = Embry0Config()
    assert cfg.default_execution_mode == "cli"
    assert cfg.default_auth_mode == "api_key"


def test_gitlab_ca_bundle_defaults_to_empty() -> None:
    """No private CA is the normal case — gitlab.com needs none."""
    cfg = Embry0Config(_env_file=None)
    assert cfg.gitlab_ca_bundle == ""


def test_gitlab_ca_bundle_reads_its_env_var(monkeypatch) -> None:
    monkeypatch.setenv("GITLAB_CA_BUNDLE", "/etc/embry0/gitlab-ca.pem")
    cfg = Embry0Config(_env_file=None)
    assert cfg.gitlab_ca_bundle == "/etc/embry0/gitlab-ca.pem"


def test_developer_log_tool_is_off_by_default() -> None:
    """SH-P4.4: a code-writing agent reading production logs is automated
    behaviour with no human in the loop — it ships OFF (wave-1 rule 1)."""
    cfg = Embry0Config(_env_file=None)
    assert cfg.developer_log_tool_enabled is False


def test_developer_log_tool_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("DEVELOPER_LOG_TOOL_ENABLED", "true")
    cfg = Embry0Config(_env_file=None)
    assert cfg.developer_log_tool_enabled is True
