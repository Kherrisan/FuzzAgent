"""hc send consults the terminal status before typing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.runtime.hc import HC, HCError, Terminal, herdr_holder_socket, pin_hc_state_home


def _hc_with(status: str) -> tuple[HC, list[list[str]]]:
    hc = HC(binary="hc")
    calls: list[list[str]] = []

    def get(name: str) -> Terminal:
        return Terminal(address="t1", name=name, state="live", status={"state": status}, turn=1)

    def invoke(args: list[str], timeout: int = 60):
        calls.append(args)
        return {"started": True}

    hc.get = get  # type: ignore[method-assign]
    hc._invoke = invoke  # type: ignore[method-assign]
    return hc, calls


@pytest.mark.parametrize("status", ["idle", "done", "blocked", "error"])
def test_send_when_terminal_is_not_working(status: str) -> None:
    hc, calls = _hc_with(status)
    assert hc.send("manageragent", "next round") == {"started": True}
    assert calls == [["send", "manageragent", "next round"]]


def test_send_refuses_while_terminal_is_working() -> None:
    hc, calls = _hc_with("working")
    with pytest.raises(HCError, match="is working"):
        hc.send("manageragent", "next round", if_not_working=True)
    assert calls == []


def test_long_role_home_pins_hc_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    home = "/root/FuzzAgent/output/wolfssl/.campaign/agents/ManagerAgent/home"
    session = "fz-wolfssl"
    monkeypatch.setenv("HOME", home)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("src.runtime.hc.Path.home", lambda: Path(home))
    assert len(herdr_holder_socket(f"{home}/.local/state", session)) == 132
    hc = HC(binary="hc", session=session)
    assert hc.env["XDG_STATE_HOME"] == "/root/.local/state"


def test_short_home_keeps_hc_state_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/root")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    hc = HC(binary="hc", session="fz-wolfssl")
    assert "XDG_STATE_HOME" not in hc.env


def test_explicit_state_home_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/root/FuzzAgent/output/wolfssl/.campaign/agents/ManagerAgent/home")
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/hc-state")
    env = dict(os.environ)
    pin_hc_state_home(env)
    assert env["XDG_STATE_HOME"] == "/tmp/hc-state"


def test_send_without_if_not_working_types_while_working() -> None:
    hc, calls = _hc_with("working")
    assert hc.send("manageragent", "next round", if_not_working=False) == {"started": True}
    assert calls == [["send", "manageragent", "next round"]]
