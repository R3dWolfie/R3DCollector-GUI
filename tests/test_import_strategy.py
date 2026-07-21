"""OsuLazerImporter import strategy:

- osu! closed  -> ONE batched launch (that process is the primary and imports
  the whole set in-process).
- osu! running -> one file per launch, serial, with retries (a batched launch
  would be a secondary instance that forwards over osu!'s single-lane import
  IPC with a hard ~3s/file timeout and drop maps).
"""
from unittest.mock import MagicMock

import osu_collector_gui as g


class _FakeProc:
    def __init__(self, rc=0):
        self._rc = rc
        self.killed = False

    def wait(self, timeout=None):
        return self._rc

    def kill(self):
        self.killed = True


def _importer(tmp_path):
    binpath = tmp_path / "osu!"
    binpath.write_text("#!/bin/sh\n")
    imp = g.OsuLazerImporter(binary_override=binpath)
    assert imp.binary is not None
    return imp


def _osz(tmp_path, n):
    out = []
    for i in range(n):
        p = tmp_path / f"{i}.osz"
        p.write_text("x")
        out.append(p)
    return out


def test_cold_launch_batches_all_files_in_one_launch(tmp_path, monkeypatch):
    imp = _importer(tmp_path)
    monkeypatch.setattr(imp, "is_running", lambda: False)
    calls = []
    monkeypatch.setattr(imp, "_launch_with_files",
                        lambda batch: calls.append(list(batch)) or _FakeProc(0))
    n = imp.import_files(_osz(tmp_path, 5))
    assert n == 5
    assert len(calls) == 1          # single launch -> primary imports the batch
    assert len(calls[0]) == 5


def test_running_forwards_one_file_per_launch(tmp_path, monkeypatch):
    imp = _importer(tmp_path)
    monkeypatch.setattr(imp, "is_running", lambda: True)
    calls = []
    monkeypatch.setattr(imp, "_launch_with_files",
                        lambda batch: calls.append(list(batch)) or _FakeProc(0))
    n = imp.import_files(_osz(tmp_path, 4))
    assert n == 4
    assert len(calls) == 4          # one launch per file, serial
    assert all(len(c) == 1 for c in calls)


def test_running_retries_transient_ipc_timeout(tmp_path, monkeypatch):
    imp = _importer(tmp_path)
    monkeypatch.setattr(imp, "is_running", lambda: True)
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)   # no real delay
    seq = [_FakeProc(1), _FakeProc(1), _FakeProc(0)]        # fail, fail, succeed
    monkeypatch.setattr(imp, "_launch_with_files", lambda batch: seq.pop(0))
    n = imp.import_files(_osz(tmp_path, 1))
    assert n == 1
    assert seq == []               # all three attempts consumed


def test_running_gives_up_after_attempts_and_counts_honestly(tmp_path, monkeypatch):
    imp = _importer(tmp_path)
    monkeypatch.setattr(imp, "is_running", lambda: True)
    monkeypatch.setattr(g.time, "sleep", lambda *_: None)
    monkeypatch.setattr(imp, "_launch_with_files", lambda batch: _FakeProc(1))  # always time out
    n = imp.import_files(_osz(tmp_path, 3))
    assert n == 0                  # honest: nothing confirmed dispatched


def test_hung_forward_is_killed_not_duplicated(tmp_path, monkeypatch):
    imp = _importer(tmp_path)
    monkeypatch.setattr(imp, "is_running", lambda: True)
    proc = _FakeProc()
    proc.wait = MagicMock(side_effect=TimeoutError)   # never exits in time
    monkeypatch.setattr(imp, "_launch_with_files", lambda batch: proc)
    n = imp.import_files(_osz(tmp_path, 1))
    assert n == 1                  # treated as forwarded
    assert proc.killed is True     # straggler killed, no duplicate spawned
