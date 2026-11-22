"""copy_project_source replaces the hc runtime's $SRC symlink."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE = Path("src/tools/builder/bin/project_utils.py")


def _load():
    spec = importlib.util.spec_from_file_location("builder_project_utils", _MODULE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_copy_project_source_replaces_symlink(tmp_path, monkeypatch):
    module = _load()
    incoming = tmp_path / "src" / "wolfssl"
    incoming.mkdir(parents=True)
    (incoming / "configure.ac").write_text("fresh\n")
    previous = tmp_path / "campaign-source"
    previous.mkdir()
    (previous / "configure.ac").write_text("old\n")
    destination = tmp_path / "root-src"
    destination.mkdir()
    link = destination / "wolfssl"
    link.symlink_to(previous)

    monkeypatch.setattr(module, "_in_src_dir", str(tmp_path / "src"))
    monkeypatch.setattr(module, "_src_dir", str(destination))
    module.ProjectUtils.copy_project_source()

    assert not link.is_symlink()
    assert (link / "configure.ac").read_text() == "fresh\n"
    assert previous.is_dir()
    assert module.os.environ["SRC"] == str(link)
