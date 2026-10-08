from __future__ import annotations

import pytest

from jtr import config

# Every jtr setting is read through the process environment (populated from
# .env), so a leaked value from the developer's own config would quietly
# change what these tests assert. Clear the lot and point at a tmp dir.
_KEYS = (
    config.KEY_BASE_URL,
    config.KEY_PAT,
    config.KEY_EMAIL,
    config.KEY_PROJECT,
    config.KEY_AUTH_METHOD,
    config.KEY_DEPLOYMENT,
    config.KEY_API_VERSION,
    config.KEY_BROWSER_CHANNEL,
    config.KEY_CONFIG_DIR,
)


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(config.KEY_CONFIG_DIR, str(tmp_path))
    # Keep `jtr skill ... --global` out of the developer's real home dirs:
    # ~/.claude via its own override, every other agent via HOME itself.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    config.ensure_env_file()
    return tmp_path
