"""Tests for dotenv export into the process environment.

pydantic-settings reads ``.env`` into the ``Settings`` model only (the
``GATEWAY_*`` fields). Provider credentials (``api_key_env`` names such
as ``OPENAI_API_KEY``) and the ``OTEL_*`` endpoints are resolved from
``os.environ`` at runtime — ``load_dotenv_exports`` (called from
``get_settings()``) is what makes the file visible to them, with real
environment variables winning.
"""

from __future__ import annotations

from gateway.config import (
    GatewayConfig,
    ProviderConfig,
    get_settings,
    load_dotenv_exports,
)

PROBE_VARS = [
    "GW_DOTENV_PLAIN",
    "GW_DOTENV_QUOTED",
    "GW_DOTENV_SINGLE",
    "GW_DOTENV_SPACED",
    "GW_DOTENV_EMPTY",
    "GW_DOTENV_PROVIDER_KEY",
    "GW_DOTENV_WIRE_PROBE",
]


def _clear_probes(monkeypatch) -> None:
    # Register every probe name with monkeypatch *before* the export
    # runs: teardown then restores the pre-test environment (absent),
    # no matter what the export set.
    for var in PROBE_VARS:
        monkeypatch.delenv(var, raising=False)


def test_load_dotenv_exports_parses_and_respects_precedence(tmp_path, monkeypatch):
    import os

    _clear_probes(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "GW_DOTENV_PLAIN=alpha\n"
        'GW_DOTENV_QUOTED="beta gamma"\n'
        "GW_DOTENV_SINGLE='charlie'\n"
        "GW_DOTENV_SPACED = delta \n"
        "GW_DOTENV_EMPTY=\n"
        "not-a-assignment line\n"
    )
    monkeypatch.setenv("GW_DOTENV_PLAIN", "real-env-wins")

    load_dotenv_exports(env_file)

    assert os.environ["GW_DOTENV_PLAIN"] == "real-env-wins"  # env beats file
    assert os.environ["GW_DOTENV_QUOTED"] == "beta gamma"
    assert os.environ["GW_DOTENV_SINGLE"] == "charlie"
    assert os.environ["GW_DOTENV_SPACED"] == "delta"
    assert "GW_DOTENV_EMPTY" not in os.environ  # empty = unset
    # The malformed line and the probes not in this file set nothing.
    assert "not-a-assignment" not in os.environ
    assert "GW_DOTENV_PROVIDER_KEY" not in os.environ
    assert "GW_DOTENV_WIRE_PROBE" not in os.environ


def test_load_dotenv_exports_missing_file_is_noop(tmp_path, monkeypatch):
    import os

    _clear_probes(monkeypatch)
    before = dict(os.environ)
    load_dotenv_exports(tmp_path / "does-not-exist.env")
    assert os.environ == before


def test_provider_key_resolves_from_dotenv(tmp_path, monkeypatch):
    """The user-facing contract: a provider credential placed in .env
    (per .env.example) is what config.yaml's api_key_env resolves."""
    import os

    _clear_probes(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_text("GW_DOTENV_PROVIDER_KEY=sk-live-from-dotenv\n")
    load_dotenv_exports(env_file)

    provider = ProviderConfig(api_base="https://api.example.com",
                             api_key_env="GW_DOTENV_PROVIDER_KEY")
    assert provider.resolve_key() == "sk-live-from-dotenv"
    assert os.environ["GW_DOTENV_PROVIDER_KEY"] == "sk-live-from-dotenv"


def test_get_settings_exports_dotenv_into_environ(tmp_path, monkeypatch):
    """get_settings() — the production path (create_app calls it when
    no Settings were injected) — makes the dotenv file visible to the
    environment before Settings parses its own fields."""
    import os

    _clear_probes(monkeypatch)
    # A fresh cwd with its own .env: get_settings must not see the
    # developer's real one, and Settings must stay constructable.
    (tmp_path / ".env").write_text("GW_DOTENV_WIRE_PROBE=wired\n")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        get_settings()
        assert os.environ["GW_DOTENV_WIRE_PROBE"] == "wired"
    finally:
        get_settings.cache_clear()


def test_yaml_config_still_loads(tmp_path):
    """Guard: the config module's public surface is unchanged — the
    YAML layer (providers, models) loads exactly as before."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "providers:\n"
        "  openai:\n"
        "    api_base: https://api.example.com/v1\n"
        "    api_key_env: OPENAI_API_KEY\n"
        "models: []\n"
    )
    cfg = GatewayConfig.load(cfg_file)
    assert cfg.providers["openai"].api_key_env == "OPENAI_API_KEY"
