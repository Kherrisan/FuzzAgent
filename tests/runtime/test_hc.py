"""hc send consults the terminal status before typing."""

from __future__ import annotations

import pytest

from src.runtime.hc import HC, HCError, Terminal


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


def test_send_without_if_not_working_types_while_working() -> None:
    hc, calls = _hc_with("working")
    assert hc.send("manageragent", "next round", if_not_working=False) == {"started": True}
    assert calls == [["send", "manageragent", "next round"]]
