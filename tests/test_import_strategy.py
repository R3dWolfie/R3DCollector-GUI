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


# ---- per-download streamed import -----------------------------------------

def test_streamed_forwards_with_retries_when_running(tmp_path, monkeypatch):
    imp = _importer(tmp_path)
    monkeypatch.setattr(imp, "is_running", lambda: True)
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    rcs = iter([1, 0])
    calls = []
    monkeypatch.setattr(imp, "_launch_with_files",
                        lambda batch: calls.append(list(batch)) or _FakeProc(next(rcs)))
    [f] = _osz(tmp_path, 1)
    assert imp.import_streamed(f) is True
    assert calls == [[str(f)], [str(f)]]      # failed once, retried


def test_streamed_cold_launch_warms_up_instead_of_waiting_forever(tmp_path, monkeypatch):
    imp = _importer(tmp_path)
    monkeypatch.setattr(imp, "is_running", lambda: False)
    waits = []

    class _Primary(_FakeProc):
        def wait(self, timeout=None):
            waits.append(timeout)
            raise g.subprocess.TimeoutExpired("osu!", timeout)

    monkeypatch.setattr(imp, "_launch_with_files", lambda batch: _Primary())
    [f] = _osz(tmp_path, 1)
    assert imp.import_streamed(f, warmup_s=7) is True
    assert waits == [7]


def test_downloader_imports_each_map_as_it_arrives(tmp_path, monkeypatch):
    job = g.DownloadJob(collection_ids=[1], output_dir=tmp_path, auto_import=True,
                        osu_binary=str(_importer(tmp_path).binary))
    d = g.Downloader(job, emit=lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(d.importer, "import_streamed",
                        lambda p: seen.append(p) or p.name != "1.osz")
    files = _osz(tmp_path, 3)
    for f in files:
        d._maybe_import(f)
    d._flush_imports()
    assert seen == files                       # one at a time, in order
    assert d._import_calls_issued == 2 and d._import_failed == 1
    d._flush_imports()                         # idempotent once drained
