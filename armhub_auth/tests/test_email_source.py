"""Tests for choosing where the add-on reads the emailed code."""
import importlib
import json
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def webui(tmp_path, monkeypatch):
    """Import the add-on's web server with its heavy dependencies stubbed out."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path.parent))
    for name in ("armadillo", "websocket", "requests", "cryptography", "cryptography.fernet", "dotenv"):
        monkeypatch.setitem(sys.modules, name, MagicMock())
    monkeypatch.syspath_prepend("armhub_auth")
    sys.modules.pop("webui", None)
    module = importlib.import_module("webui")
    monkeypatch.setattr(module, "_gmail_thread", None)
    monkeypatch.setattr(module, "_gmail_watcher", MagicMock())
    monkeypatch.setattr(module, "_start_imap_watcher", MagicMock())
    yield module
    sys.modules.pop("webui", None)


def set_options(module, **options):
    module.OPTIONS_FILE.write_text(json.dumps(options))


def connect_gmail(module):
    set_options(module, **{**module._options(), "gmail_client_id": "id", "gmail_client_secret": "secret"})
    module._gmail_tok["refresh_token"] = "token"


def test_defaults_to_gmail(webui):
    assert webui._email_source() == "gmail"
    assert not webui._email_source_ready()


def test_gmail_source_never_falls_back_to_imap(webui):
    set_options(webui, email_code_source="gmail", imap_entity="sensor.imap")
    assert not webui._email_source_ready()
    webui._start_mfa_watcher()
    webui._start_imap_watcher.assert_not_called()


def test_gmail_source_uses_gmail_once_connected(webui):
    set_options(webui, email_code_source="gmail", imap_entity="sensor.imap")
    connect_gmail(webui)
    assert webui._email_source_ready()
    webui._start_mfa_watcher()
    webui._start_imap_watcher.assert_not_called()
    webui._gmail_thread.join(1)
    webui._gmail_watcher.assert_called_once()


def test_imap_source_ignores_a_connected_gmail(webui):
    set_options(webui, email_code_source="imap", imap_entity=" sensor.imap ")
    connect_gmail(webui)
    webui._start_mfa_watcher()
    webui._start_imap_watcher.assert_called_once_with("sensor.imap")
    webui._gmail_watcher.assert_not_called()


def test_imap_source_needs_a_sensor(webui):
    set_options(webui, email_code_source="imap", imap_entity="")
    assert not webui._email_source_ready()


def test_manual_source_starts_nothing(webui):
    set_options(webui, email_code_source="manual", imap_entity="sensor.imap")
    connect_gmail(webui)
    assert not webui._email_source_ready()
    webui._start_mfa_watcher()
    webui._start_imap_watcher.assert_not_called()
    webui._gmail_watcher.assert_not_called()
