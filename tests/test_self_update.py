"""Updater paths: git fast-forward for source checkouts, tarball swap for the
packaged Linux build, and CM CLI self-upgrade on a realm schema mismatch."""
import subprocess
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import osu_collector_gui as g


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    """An 'upstream' repo and a clone of it that is one commit behind."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main")
    (up / "app.py").write_text("v1\n")
    _git(up, "add", "-A"); _git(up, "commit", "-qm", "v1")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(up), str(clone))
    (up / "app.py").write_text("v2\n")
    _git(up, "add", "-A"); _git(up, "commit", "-qm", "v2")
    return up, clone


def test_source_update_fast_forwards(checkout):
    up, clone = checkout
    res = g._update_source_checkout(clone)
    assert res["ok"] and res["restart"]
    assert (clone / "app.py").read_text() == "v2\n"
    assert _git(clone, "rev-parse", "HEAD") == _git(up, "rev-parse", "HEAD")


def test_source_update_refuses_dirty_tree(checkout):
    _, clone = checkout
    (clone / "app.py").write_text("local edit\n")
    res = g._update_source_checkout(clone)
    assert not res["ok"] and "uncommitted" in res["error"]
    assert (clone / "app.py").read_text() == "local edit\n"   # untouched


def test_source_update_reports_release_not_on_branch(checkout):
    _, clone = checkout
    assert g._update_source_checkout(clone)["ok"]
    res = g._update_source_checkout(clone, target="9.9.9")
    assert not res["ok"]
    assert "already at its newest commit" in res["error"]
    assert "v9.9.9 isn't on it" in res["error"]


def test_source_update_never_rewrites_diverged_history(checkout):
    _, clone = checkout
    (clone / "local.txt").write_text("mine\n")
    _git(clone, "add", "-A"); _git(clone, "commit", "-qm", "local work")
    head = _git(clone, "rev-parse", "HEAD")
    res = g._update_source_checkout(clone)
    assert not res["ok"] and "git pull failed" in res["error"]
    assert _git(clone, "rev-parse", "HEAD") == head


def test_tarball_update_swaps_install_even_if_folder_was_renamed(tmp_path, monkeypatch):
    # Installed build lives in a folder the user renamed.
    install = tmp_path / "my-collector"
    install.mkdir()
    (install / "osu-collector-gui").write_text("old")
    # Release archive uses the stock top-level folder name.
    src = tmp_path / "build" / "osu-collector-gui"
    src.mkdir(parents=True)
    (src / "osu-collector-gui").write_text("new")
    tar_path = tmp_path / "dl" / "update.tar.gz"
    tar_path.parent.mkdir()
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(src, arcname="osu-collector-gui")

    launched = []
    monkeypatch.setattr(g.sys, "frozen", True, raising=False)
    monkeypatch.setattr(g.sys, "executable", str(install / "osu-collector-gui"))
    monkeypatch.setattr(g, "_relaunch", lambda argv, cwd: launched.append((argv, cwd)))
    monkeypatch.setattr(g.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))

    with pytest.raises(SystemExit):
        g._apply_linux_tarball_update(tar_path)

    assert (install / "osu-collector-gui").read_text() == "new"
    assert launched == [([str(install / "osu-collector-gui")], install)]
    assert len(list(tmp_path.glob("my-collector.bak-*"))) == 1   # rollback copy
    assert not list(tmp_path.glob(".ocg-upd-*"))                 # staging cleaned


def _proc(rc=0, out="", err=""):
    p = MagicMock(); p.returncode = rc; p.stdout = out; p.stderr = err
    return p


_MISMATCH = ("RealmNotValidatedException: Opening osu!lazer database failed. "
             "Expected schema version: '51', got: '52'.")


def test_cm_cli_schema_mismatch_upgrades_cached_cli_and_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "CM_CLI_CACHE_DIR", tmp_path)
    monkeypatch.setattr(g.CmCliRunner, "DEBUG_LOG", tmp_path / "debug.log")
    monkeypatch.setattr(g.CmCliRunner, "_upgrade_tried", False)
    installs = []
    monkeypatch.setattr(g.CmCliInstaller, "install",
                        staticmethod(lambda log_func=print: installs.append(1)))
    results = iter([_proc(1, err=_MISMATCH), _proc(0)])
    runs = []
    monkeypatch.setattr(g.subprocess, "run",
                        lambda argv, **kw: runs.append(argv) or next(results))
    exe = str(tmp_path / "CollectionManager.App.Cli.exe")

    g.CmCliRunner._run(["wine", exe, "convert"])     # no exception: self-healed

    assert installs == [1]
    assert runs == [["wine", exe, "convert"]] * 2


def test_cm_cli_schema_mismatch_explains_when_no_newer_cli_helps(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "CM_CLI_CACHE_DIR", tmp_path)
    monkeypatch.setattr(g.CmCliRunner, "DEBUG_LOG", tmp_path / "debug.log")
    monkeypatch.setattr(g.CmCliRunner, "_upgrade_tried", False)
    monkeypatch.setattr(g.CmCliInstaller, "install",
                        staticmethod(lambda log_func=print: None))
    monkeypatch.setattr(g.subprocess, "run",
                        lambda argv, **kw: _proc(1, err=_MISMATCH))
    exe = str(tmp_path / "CollectionManager.App.Cli.exe")
    with pytest.raises(RuntimeError, match="newer than Collection Manager supports"):
        g.CmCliRunner._run(["wine", exe, "convert"])
