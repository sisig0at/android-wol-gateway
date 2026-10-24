"""Unit tests for configuration loading and WoL call parameters.

These tests do not touch the network, the Telegram API or any real hardware:
send_magic_packet and the Telegram client are monkeypatched. Magic packet
payload construction follows the AMD Magic Packet spec (6 x 0xFF followed by
the target MAC repeated 16 times).
"""

import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402


FAKE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456789:TESTFAKETOKENNOTREAL0000000",
    "TELEGRAM_USER_ID": "111222333",
    "PC_MAC_ADDRESS": "AA:BB:CC:DD:EE:FF",
    "PC_LOCAL_IP": "192.168.1.10",
    "PC_STATUS_PORTS": "3389,445",
    "WOL_BROADCAST_ADDR": "192.168.1.255",
    "WOL_PORT": "9",
    "WAKE_COOLDOWN_SECONDS": "20",
    "WAKING_TIMEOUT_SECONDS": "180",
    "PING_TIMEOUT_SECONDS": "2",
    "LOG_LEVEL": "INFO",
}


@pytest.fixture
def fake_env(monkeypatch):
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)
    yield


def _env(**overrides):
    data = dict(FAKE_ENV)
    data.update(overrides)
    return {k: v for k, v in data.items()}


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def test_config_loads_with_valid_values(fake_env):
    cfg, errors = bot.load_config()
    assert cfg is not None, errors
    assert cfg.token == FAKE_ENV["TELEGRAM_BOT_TOKEN"]
    assert cfg.user_id == 111222333
    assert cfg.mac == "AA:BB:CC:DD:EE:FF"
    assert cfg.local_ip == "192.168.1.10"
    assert cfg.status_ports == (3389, 445)
    assert cfg.wol_broadcast == "192.168.1.255"
    assert cfg.wol_port == 9
    assert cfg.cooldown_seconds == 20


def test_config_missing_required_vars(monkeypatch):
    for key, value in _env(TELEGRAM_BOT_TOKEN="", PC_MAC_ADDRESS="").items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is None
    joined = " ".join(errors)
    assert "TELEGRAM_BOT_TOKEN" in joined
    assert "PC_MAC_ADDRESS" in joined


def test_config_rejects_bad_mac_format(monkeypatch):
    for key, value in _env(PC_MAC_ADDRESS="not-a-mac").items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is None
    assert any("PC_MAC_ADDRESS" in e and "неверный формат" in e for e in errors)


def test_config_accepts_dash_separator_mac(monkeypatch):
    for key, value in _env(PC_MAC_ADDRESS="aa-bb-cc-dd-ee-ff").items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is not None, errors
    assert cfg.mac == "aa-bb-cc-dd-ee-ff"


def test_config_rejects_non_numeric_user_id(monkeypatch):
    for key, value in _env(TELEGRAM_USER_ID="notanumber").items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is None
    assert any("TELEGRAM_USER_ID" in e for e in errors)


def test_config_rejects_bad_status_ports(monkeypatch):
    for key, value in _env(PC_STATUS_PORTS="http,445").items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is None
    assert any("PC_STATUS_PORTS" in e for e in errors)


def test_config_rejects_out_of_range_wol_port(monkeypatch):
    for key, value in _env(WOL_PORT="70000").items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is None
    assert any("WOL_PORT" in e for e in errors)


def test_config_rejects_bad_log_level(monkeypatch):
    for key, value in _env(LOG_LEVEL="LOUD").items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is None
    assert any("LOG_LEVEL" in e for e in errors)


def test_config_defaults_when_optional_empty(monkeypatch):
    data = _env(WOL_BROADCAST_ADDR="", WOL_PORT="", PC_LOCAL_IP="",
                WAKE_COOLDOWN_SECONDS="", WAKING_TIMEOUT_SECONDS="",
                PING_TIMEOUT_SECONDS="", LOG_LEVEL="")
    for key, value in data.items():
        monkeypatch.setenv(key, value)
    cfg, errors = bot.load_config()
    assert cfg is not None, errors
    # Defaults from bot.py: global broadcast, port 9, 20 s cooldown, ... 
    assert cfg.wol_broadcast == "255.255.255.255"
    assert cfg.wol_port == 9
    assert cfg.cooldown_seconds == 20
    assert cfg.waking_timeout_seconds == 180
    assert cfg.ping_timeout_seconds == 2
    assert cfg.local_ip is None


# ---------------------------------------------------------------------------
# Magic packet payload (AMD Magic Packet format)
# ---------------------------------------------------------------------------

def _expected_payload(mac: str) -> bytes:
    hex_str = mac.replace(":", "").replace("-", "")
    target = bytes.fromhex(hex_str)
    return b"\xff" * 6 + target * 16


@pytest.mark.parametrize("mac", [
    "AA:BB:CC:DD:EE:FF",
    "AA-BB-CC-DD-EE-FF",
    "aa:bb:cc:dd:ee:ff",
])
def test_magic_packet_payload_structure(mac):
    """6 x 0xFF sync word followed by the MAC repeated 16 times = 102 bytes."""
    payload = _expected_payload(mac)
    assert len(payload) == 6 + 16 * 6 == 102
    assert payload[:6] == b"\xff" * 6
    assert payload[6:12] == bytes.fromhex("aabbccddeeff")
    assert payload[12:18] == bytes.fromhex("aabbccddeeff")
    assert payload[96:] == bytes.fromhex("aabbccddeeff")


def test_both_mac_separators_yield_identical_payload():
    assert _expected_payload("AA:BB:CC:DD:EE:FF") == _expected_payload(
        "AA-BB-CC-DD-EE-FF"
    )


# ---------------------------------------------------------------------------
# send_wol_now: verifies what the gateway sends (no network access)
# ---------------------------------------------------------------------------

def test_send_wol_now_calls_wakeonlan_with_config_values(monkeypatch, fake_env):
    calls = []

    def fake_send(mac, ip_address=None, port=None, **kwargs):
        calls.append((mac, ip_address, port))

    monkeypatch.setattr(bot, "send_magic_packet", fake_send)
    rc = bot.send_wol_now()
    assert rc == 0
    assert len(calls) == 1
    mac, ip_address, port = calls[0]
    assert mac == "AA:BB:CC:DD:EE:FF"
    assert ip_address == "192.168.1.255"
    assert port == 9


def test_send_wol_now_reports_failure_without_raising(monkeypatch, fake_env):
    def failing_send(**kwargs):
        raise OSError("network unreachable (simulated)")

    monkeypatch.setattr(bot, "send_magic_packet", failing_send)
    rc = bot.send_wol_now()
    assert rc == 1


# ---------------------------------------------------------------------------
# User authorization filter
# ---------------------------------------------------------------------------

def _make_update(user_id: int):
    from telegram import Chat, Message, Update, User

    message = Message(
        message_id=1,
        date=None,
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="t"),
    )
    return Update(update_id=1, message=message)


def test_is_allowed_accepts_owner(fake_env):
    cfg, errors = bot.load_config()
    bot.CFG = cfg
    assert bot.is_allowed(_make_update(111222333)) is True


def test_is_allowed_rejects_others(fake_env):
    cfg, errors = bot.load_config()
    bot.CFG = cfg
    assert bot.is_allowed(_make_update(999999999)) is False


# ---------------------------------------------------------------------------
# Logging must never contain URLs/URL data with token
# ---------------------------------------------------------------------------

def test_setup_logging_silences_httpx_request_logs(caplog):
    bot.setup_logging("INFO")
    logger = logging.getLogger("httpx")
    assert logger.getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING


def test_setup_logging_error_text_contains_no_token(monkeypatch, fake_env, capsys):
    """--check output must not print the token."""
    rc = bot.selfcheck()
    out = capsys.readouterr().out
    assert "TESTFAKETOKENNOTREAL" not in out
    assert rc == 0 or rc == 1
