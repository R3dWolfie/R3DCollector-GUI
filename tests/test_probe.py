"""Verify CmCliRunner.probe_imported_beatmaps invokes CM CLI correctly and parses results."""
from pathlib import Path
from unittest.mock import patch, MagicMock

from osu_collector_gui import (
    CmCliConfig, CmCliRunner, ProbeResult,
    OsdbWriter, CollectionInfo, BeatmapInfo,
)


def _write_fake_probe_osdb(dest: Path, resolved_beatmaps: list[BeatmapInfo]) -> None:
    """Write a probe.osdb shaped like what `cm create -b` would produce."""
    info = CollectionInfo(
        id=0, name="probe", uploader="cm-cli",
        beatmap_count=len(resolved_beatmaps),
        beatmaps=resolved_beatmaps,
    )
    OsdbWriter.write(dest, info)


def _no_interactive() -> MagicMock:
    """What a pre-1.3.0 CM CLI does with the unknown `interactive` verb."""
    r = MagicMock(); r.returncode = 1; r.stdout = ""; r.stderr = ""
    return r


def test_probe_writes_bids_to_realm_parent_and_returns_resolved(tmp_path):
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")
    tmp_dir = realm.parent / ".oc-gui-tmp"

    captured_argv: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv[-1] == "interactive":
            return _no_interactive()
        captured_argv.append(argv)
        # Find the `-o` argument and write a fake probe.osdb there.
        out_idx = argv.index("-o") + 1
        out_path = Path(argv[out_idx])
        _write_fake_probe_osdb(out_path, [
            BeatmapInfo(beatmap_id=42, set_id=100, md5="lazer-md5-aaa",
                        artist="A", title="T", diff_name="D"),
        ])
        result = MagicMock()
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    cfg = CmCliConfig(command=["/fake/cm.exe"], osu_location=None)
    runner = CmCliRunner(cfg)

    with patch("osu_collector_gui.subprocess.run", side_effect=fake_run):
        result = runner.probe_imported_beatmaps(realm, [42, 99])

    # Returned ProbeResult contains lazer's md5 for the resolved bid.
    assert isinstance(result, ProbeResult)
    assert 42 in result.resolved
    assert result.resolved[42].md5 == "lazer-md5-aaa"
    assert 99 not in result.resolved

    # Bids file was written under the realm's parent tmp dir.
    [argv] = captured_argv
    bids_idx = argv.index("-b") + 1
    bids_path = Path(argv[bids_idx])
    assert bids_path.parent == tmp_dir
    assert "42" in bids_path.read_text()
    assert "99" in bids_path.read_text()

    # -l points at the snapshot subdir under .oc-gui-tmp/, NOT the live
    # realm's parent — the probe takes a file-level copy of client.realm
    # so CM CLI doesn't contend with a running osu!lazer for the lock.
    l_idx = argv.index("-l") + 1
    l_path = Path(argv[l_idx])
    assert l_path == tmp_dir / "probe-realm"
    # CM CLI looks for a file literally named "client.realm" in the -l dir.
    # The snapshot was created with that filename (we can verify by checking
    # that fake_run saw the snapshot during the call — at that moment the
    # file existed; the finally block unlinks it after probe_imported_beatmaps
    # returns).
    # Note: we don't assert the snapshot still exists post-return because
    # the finally block deliberately cleans it up.


def test_probe_matches_by_hash_including_onlineid_minus_one(tmp_path):
    """Hash matching must catch maps lazer imported with no positive online id
    (e.g. mirror imports it couldn't verify) as long as they have real
    metadata — id matching alone would miss them."""
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")
    captured: list[list[str]] = []

    def fake_run(argv, **kwargs):
        if argv[-1] == "interactive":
            return _no_interactive()
        captured.append(argv)
        out_path = Path(argv[argv.index("-o") + 1])
        _write_fake_probe_osdb(out_path, [
            BeatmapInfo(beatmap_id=42, set_id=100, md5="hash-ranked",
                        artist="A", title="T", diff_name="D"),
            # OnlineID unknown but real metadata -> recognized via hash.
            BeatmapInfo(beatmap_id=0, set_id=200, md5="hash-unverified",
                        artist="RealArtist", title="RealTitle", diff_name="D"),
            # Not in lazer -> Unknown metadata, id 0 -> NOT recognized.
            BeatmapInfo(beatmap_id=0, set_id=300, md5="hash-missing",
                        artist="Unknown", title="Unknown", diff_name="D"),
        ])
        r = MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    runner = CmCliRunner(CmCliConfig(command=["/fake/cm.exe"], osu_location=None))
    with patch("osu_collector_gui.subprocess.run", side_effect=fake_run):
        result = runner.probe_imported_beatmaps(
            realm, [], hashes=["hash-ranked", "hash-unverified", "hash-missing"])

    [argv] = captured
    assert "-h" in argv                       # probed by hash, not id
    assert "hash-ranked" in result.resolved_hashes
    assert "hash-unverified" in result.resolved_hashes   # the OnlineID=-1 case
    assert "hash-missing" not in result.resolved_hashes  # Unknown -> not imported


def test_probe_uses_interactive_stdin_on_cm_cli_1_3(tmp_path):
    """CM CLI 1.3.0+ dropped -b and file-path values, so ids and hashes go
    through the `interactive` REPL on stdin in a single invocation."""
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")
    tmp_dir = realm.parent / ".oc-gui-tmp"
    calls: list[tuple[list[str], str]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("input", "")))
        _write_fake_probe_osdb(tmp_dir / "probe.osdb", [
            BeatmapInfo(beatmap_id=42, set_id=100, md5="hash-ranked",
                        artist="A", title="T", diff_name="D"),
        ])
        r = MagicMock(); r.returncode = 0; r.stdout = ""; r.stderr = ""
        return r

    runner = CmCliRunner(CmCliConfig(command=["/fake/cm.exe"], osu_location=None))
    with patch("osu_collector_gui.subprocess.run", side_effect=fake_run):
        result = runner.probe_imported_beatmaps(
            realm, [42, 99], hashes=["hash-ranked", "hash-missing"])

    [(argv, script)] = calls                  # one launch, no legacy fallback
    assert argv == ["/fake/cm.exe", "interactive"]
    lines = script.splitlines()
    assert lines[0].startswith("create -h hash-ranked,hash-missing -l ")
    assert lines[1].startswith("create -i 42,99 -l ")
    assert f'"{tmp_dir / "probe-realm"}"' in lines[0]
    assert lines[2] == f'save -o "{tmp_dir / "probe.osdb"}"'
    assert lines[3] == "exit"
    assert 42 in result.resolved
    assert "hash-ranked" in result.resolved_hashes


def test_probe_returns_empty_result_when_cm_cli_fails(tmp_path):
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")

    def fake_run(argv, **kwargs):
        # Write an output file but exit non-zero — _run raises RuntimeError.
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        result.stderr = "wine crash"
        return result

    cfg = CmCliConfig(command=["/fake/cm.exe"], osu_location=None)
    runner = CmCliRunner(cfg)

    with patch("osu_collector_gui.subprocess.run", side_effect=fake_run):
        result = runner.probe_imported_beatmaps(realm, [42])

    assert result.resolved == {}


def test_probe_handles_empty_beatmap_id_list(tmp_path):
    realm = tmp_path / "client.realm"
    realm.write_bytes(b"fake")

    cfg = CmCliConfig(command=["/fake/cm.exe"], osu_location=None)
    runner = CmCliRunner(cfg)

    with patch("osu_collector_gui.subprocess.run") as run_mock:
        result = runner.probe_imported_beatmaps(realm, [])

    assert result.resolved == {}
    # Should not have invoked CM CLI for an empty list.
    run_mock.assert_not_called()
