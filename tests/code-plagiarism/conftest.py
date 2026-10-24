"""Shared pytest setup: tests never touch the network."""
import socket

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def _blocked(*args, **kwargs):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)


_REGISTRY_ENV = ("NPM_CONFIG_REGISTRY", "npm_config_registry", "YARN_NPM_REGISTRY_SERVER", "PIP_INDEX_URL",
                 "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_CONFIG_FILE", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL",
                 "UV_DEFAULT_INDEX", "UV_INDEX")


@pytest.fixture(autouse=True)
def _isolated_user_config(tmp_path_factory, monkeypatch):
    """The developer's own ~/.npmrc, pip.conf and registry variables must not change test outcomes."""
    home = tmp_path_factory.mktemp("home")
    for var in ("HOME", "USERPROFILE", "APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.setenv(var, str(home))
    for var in _REGISTRY_ENV:
        monkeypatch.delenv(var, raising=False)
    return home
