"""osu! stable support: native collection.db I/O, install discovery, Songs
import, Songs-based skip and the collection merge (waits for osu! to close)."""
from pathlib import Path

import pytest

import osu_collector_gui as g

H1, H2, H3 = "a" * 32, "b" * 32, "c" * 32


def _install(tmp_path: Path) -> Path:
    root = tmp_path / "osu!"
    (root / "Songs").mkdir(parents=True)
    (root / "osu!.exe").write_bytes(b"MZ")
    return root


# ---- collection.db ---------------------------------------------------------

def test_collection_db_round_trip(tmp_path):
    db = tmp_path / "collection.db"
    cols = [("Streams", [H1, H2]), ("ünïcode ★", [H3]), ("empty", [])]
    g.StableCollectionDb.write(db, cols, 20250107)
    assert g.StableCollectionDb.read(db) == (20250107, cols)
    assert not list(tmp_path.glob("*.oc-tmp"))           # atomic write cleaned up


def test_collection_db_reads_null_strings_and_long_names(tmp_path):
    db = tmp_path / "collection.db"
    long = "x" * 300                                     # 2-byte ULEB128 length
    g.StableCollectionDb.write(db, [(long, [H1])])
    raw = bytearray(db.read_bytes())
    raw += b"\x00" + (1).to_bytes(4, "little") + b"\x00"  # a null-named collection
    raw[4:8] = (2).to_bytes(4, "little")
    db.write_bytes(bytes(raw))
    _v, cols = g.StableCollectionDb.read(db)
    assert cols == [(long, [H1]), ("", [""])]


def test_missing_collection_db_is_empty(tmp_path):
    assert g.StableCollectionDb.read(tmp_path / "nope.db")[1] == []


def test_merge_unions_by_name_without_duplicates():
    merged, added = g.StableCollectionDb.merge(
        [("A", [H1]), ("B", [H2])],
        [("A", [H1, H3]), ("C", [H2, H2])])
    assert merged == [("A", [H1, H3]), ("B", [H2]), ("C", [H2])]
    assert added == 2


# ---- install -----------------------------------------------------------------

def test_locate_override_accepts_folder_or_exe(tmp_path):
    root = _install(tmp_path)
    assert g.OsuStable.locate(str(root)).root == root
    assert g.OsuStable.locate(str(root / "osu!.exe")).root == root
    assert g.OsuStable.locate(str(tmp_path)) is None     # no osu!.exe there


def test_songs_dir_honours_cfg(tmp_path):
    root = _install(tmp_path)
    st = g.OsuStable(root)
    assert st.songs_dir == root / "Songs"
    (root / "Beatmaps").mkdir()
    (root / "osu!.red.cfg").write_text("Volume = 50\nBeatmapDirectory = Beatmaps\n")
    assert st.songs_dir == root / "Beatmaps"
    (root / "osu!.red.cfg").write_text("BeatmapDirectory = E:\\Songs\n")
    assert st.songs_dir == root / "Songs"                # foreign drive: fall back


def test_installed_set_ids_reads_folders_and_pending_osz(tmp_path):
    root = _install(tmp_path)
    songs = root / "Songs"
    (songs / "123 Artist - Title").mkdir()
    (songs / "456.osz").write_bytes(b"x")
    (songs / "789 Pending - Map.osz").write_bytes(b"x")
    (songs / "Failed").mkdir()
    assert g.OsuStable(root).installed_set_ids() == {123, 456, 789}


def test_import_file_moves_into_songs(tmp_path):
    root = _install(tmp_path)
    osz = tmp_path / "dl" / "123 A - B.osz"
    osz.parent.mkdir()
    osz.write_bytes(b"osz")
    assert g.OsuStable(root).import_file(osz)
    assert (root / "Songs" / osz.name).read_bytes() == b"osz"
    assert not osz.exists()


def test_merge_collections_backs_up_and_writes(tmp_path):
    root = _install(tmp_path)
    st = g.OsuStable(root)
    g.StableCollectionDb.write(st.collection_db, [("Mine", [H1])], 20240101)
    added, backup = st.merge_collections([("Mine", [H2]), ("New", [H3])])
    assert added == 2 and backup.exists()
    assert g.StableCollectionDb.read(backup)[1] == [("Mine", [H1])]
    assert g.StableCollectionDb.read(st.collection_db) == (
        20240101, [("Mine", [H1, H2]), ("New", [H3])])
    assert st.merge_collections([("Mine", [H1])]) == (0, None)   # no-op, no backup


# ---- downloader in stable mode ---------------------------------------------

def _downloader(tmp_path, root, **kw):
    job = g.DownloadJob(collection_ids=[1], output_dir=tmp_path / "out",
                        client="stable", stable_dir=str(root), **kw)
    events = []
    return g.Downloader(job, emit=lambda e, d: events.append((e, d))), events


def test_downloader_moves_maps_into_songs(tmp_path):
    root = _install(tmp_path)
    d, _ = _downloader(tmp_path, root)
    osz = tmp_path / "1 A - B.osz"
    osz.write_bytes(b"x")
    d._maybe_import(osz)
    d._flush_imports()
    assert (root / "Songs" / osz.name).exists()
    assert d._import_calls_issued == 1


def test_stable_mode_needs_no_cli_for_skip_and_merge(tmp_path):
    root = _install(tmp_path)
    d, _ = _downloader(tmp_path, root, add_to_stable_collections=True)
    assert d._probe_enabled_for_job()
    assert d._should_fetch_details()           # md5s for collection.db
    assert not d._should_generate_osdb()       # no CM CLI round-trip


def test_stable_merge_waits_for_osu_to_close(tmp_path, monkeypatch):
    root = _install(tmp_path)
    d, events = _downloader(tmp_path, root, add_to_stable_collections=True)
    d._stable_collections = [("Streams", [H1])]
    running = iter([True, True, False])
    monkeypatch.setattr(d.stable, "is_running", lambda: next(running))
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    d._merge_into_stable()
    assert g.StableCollectionDb.read(d.stable.collection_db)[1] == [("Streams", [H1])]
    assert any(e == "prep" for e, _ in events)


def test_stable_merge_gives_up_on_cancel_without_writing(tmp_path, monkeypatch):
    root = _install(tmp_path)
    d, _ = _downloader(tmp_path, root, add_to_stable_collections=True)
    d._stable_collections = [("Streams", [H1])]
    monkeypatch.setattr(d.stable, "is_running", lambda: True)
    monkeypatch.setattr(g.time, "sleep", lambda s: d.cancel())
    d._merge_into_stable()
    assert not d.stable.collection_db.exists()


def test_is_running_ignores_lazer(monkeypatch):
    psutil = pytest.importorskip("psutil")

    class P:
        def __init__(self, cmd): self.info = {"name": "osu!.exe", "cmdline": cmd, "exe": ""}

    st = g.OsuStable(Path("/x"))
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [
        P([r"C:\Users\a\AppData\Local\osulazer\current\osu!.exe"])])
    assert not st.is_running()
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [
        P([r"C:\users\a\AppData\Local\osu!\osu!.exe"])])
    assert st.is_running()


def test_is_running_ignores_processes_that_merely_mention_it(monkeypatch):
    psutil = pytest.importorskip("psutil")

    class P:
        def __init__(self, cmd): self.info = {"name": "x", "cmdline": cmd, "exe": ""}

    st = g.OsuStable(Path("/x"))
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [
        P(["/usr/bin/bash", "-c", "touch ~/osu!/osu!.exe"]),
        P(["/usr/bin/python", "check-osu!.exe.py"])])
    assert not st.is_running()
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [
        P(["/home/u/.local/share/osu-wine/osu!/osu!.exe", "-devserver"])])
    assert st.is_running()
