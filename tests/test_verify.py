# -*- coding: utf-8 -*-
"""Tests for jms.verify — all remote calls mocked, local files via tmp_path."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from unittest.mock import Mock

from jms.verify import (
    LocalHasher,
    RemoteHasher,
    _parse_md5_line,
    group_tasks_by_file,
    translate_remote_path,
    verify_files,
)

CONTENT = b"jms-cli verify test payload\n" * 1000
CONTENT_MD5 = hashlib.md5(CONTENT).hexdigest()


@dataclass(frozen=True)
class FakeTask:
    """Duck-typed stand-in for jms.transfer.FileTask."""

    src_path: str
    dst_path: str
    chunk_index: int = 0
    start: int = 0
    chunk_size: int = 0
    merge_to: str = ""

    @property
    def end(self) -> int:
        return self.start + self.chunk_size


@dataclass(frozen=True)
class FakeResult:
    """Duck-typed stand-in for jms.transfer.TaskResult."""

    task: FakeTask
    md5: str


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


# ──── _parse_md5_line ────────────────────────────────────────────


def test_parse_md5_line_valid() -> None:
    assert _parse_md5_line(f"{CONTENT_MD5}  /remote/file") == CONTENT_MD5


def test_parse_md5_line_uppercase_normalized() -> None:
    assert _parse_md5_line(CONTENT_MD5.upper() + "  /f") == CONTENT_MD5


def test_parse_md5_line_garbage() -> None:
    assert _parse_md5_line("") == ""
    assert _parse_md5_line("md5sum: /x: No such file or directory") == ""
    assert _parse_md5_line("zzzz" + CONTENT_MD5[4:]) == ""


# ──── translate_remote_path ──────────────────────────────────────


def test_translate_remote_path() -> None:
    assert translate_remote_path("/", "/a/b") == "/a/b"
    assert translate_remote_path("", "/a/b") == "/a/b"
    assert translate_remote_path("./", "/a/b") == "./a/b"
    assert translate_remote_path(".", "a/b") == "./a/b"
    assert translate_remote_path("/tmp/", "/a") == "/tmp/a"


# ──── LocalHasher ────────────────────────────────────────────────


def test_local_md5_full(tmp_path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(CONTENT)
    assert LocalHasher().md5_full(str(p)) == CONTENT_MD5


def test_local_md5_full_missing_file(tmp_path) -> None:
    assert LocalHasher().md5_full(str(tmp_path / "nope")) == ""


def test_local_md5_ranges(tmp_path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(CONTENT)
    ranges = [(0, 10), (10, 20), (0, 0)]
    out = LocalHasher().md5_ranges(str(p), ranges)
    assert out == [_md5(CONTENT[0:10]), _md5(CONTENT[10:30]), ""]


# ──── RemoteHasher ───────────────────────────────────────────────


def _term(outputs: dict[str, str]) -> Mock:
    """Mock terminal: map command substring -> execute output."""
    term = Mock()

    def execute(cmd: str, timeout: int = 30) -> str:
        for key, out in outputs.items():
            if key in cmd:
                return out
        return ""

    term.execute.side_effect = execute
    return term


def test_remote_md5_full() -> None:
    term = Mock()
    term.execute.return_value = f"{CONTENT_MD5}  /data/f.bin\n"
    h = RemoteHasher(term)
    assert h.md5_full("/data/f.bin") == CONTENT_MD5
    term.execute.assert_called_once_with("md5sum /data/f.bin", timeout=1800)


def test_remote_md5_full_failure_output() -> None:
    term = Mock()
    term.execute.return_value = "md5sum: /data/f.bin: No such file or directory"
    assert RemoteHasher(term).md5_full("/data/f.bin") == ""


def test_remote_md5_full_chroot_translation() -> None:
    term = Mock()
    term.execute.return_value = f"{CONTENT_MD5}  ./f.bin"
    RemoteHasher(term, chroot="./").md5_full("/f.bin")
    term.execute.assert_called_once_with("md5sum ./f.bin", timeout=1800)


def test_remote_md5_ranges() -> None:
    d1, d2 = _md5(b"a"), _md5(b"bb")
    term = Mock()
    term.execute.return_value = f"{d1}  -\n{d2}  -\n"
    out = RemoteHasher(term).md5_ranges("/f", [(0, 1), (1, 2)])
    assert out == [d1, d2]
    cmd = term.execute.call_args[0][0]
    assert cmd.count("dd if=/f") == 2
    assert "skip=0 count=1" in cmd and "skip=1 count=2" in cmd


def test_remote_md5_ranges_empty_input() -> None:
    term = Mock()
    assert RemoteHasher(term).md5_ranges("/f", []) == []
    term.execute.assert_not_called()


def test_remote_md5_ranges_pads_missing_lines() -> None:
    term = Mock()
    term.execute.return_value = f"{_md5(b'a')}  -\n"  # only 1 of 2 answered
    out = RemoteHasher(term).md5_ranges("/f", [(0, 1), (1, 2)])
    assert out == [_md5(b"a"), ""]


def test_remote_md5_ranges_ignores_stderr_noise() -> None:
    term = Mock()
    term.execute.return_value = "dd: some warning\n" f"{_md5(b'a')}  -"
    assert RemoteHasher(term).md5_ranges("/f", [(0, 1)]) == [_md5(b"a")]


# ──── group_tasks_by_file / verify_files ─────────────────────────


def test_group_tasks_by_file_sorts_and_merges() -> None:
    t0 = FakeTask("/s/f", "/d/f.part00", chunk_index=0)
    t1 = FakeTask("/s/f", "/d/f.part01", chunk_index=1, start=10)
    t2 = FakeTask("/s/g", "/d/g", chunk_index=0)
    # no merge_to → each dst_path is its own group, insertion order
    groups = group_tasks_by_file([t1, t0, t2])
    assert list(groups) == [
        ("/s/f", "/d/f.part01"), ("/s/f", "/d/f.part00"), ("/s/g", "/d/g"),
    ]
    # merge_to makes the logical file the group key, sorted by chunk_index
    t0m = FakeTask("/s/f", "/d/f.part00", chunk_index=0, merge_to="/d/f")
    t1m = FakeTask("/s/f", "/d/f.part01", chunk_index=1, start=10, merge_to="/d/f")
    groups = group_tasks_by_file([t1m, t0m])
    assert list(groups) == [("/s/f", "/d/f")]
    assert groups[("/s/f", "/d/f")] == [t0m, t1m]


def test_verify_files_match() -> None:
    task = FakeTask("/s/f", "/d/f")
    src = Mock(spec=RemoteHasher)
    dst = Mock(spec=RemoteHasher)
    src.md5_full.return_value = CONTENT_MD5
    dst.md5_full.return_value = CONTENT_MD5
    out = verify_files([FakeResult(task, CONTENT_MD5)], src, dst)
    assert len(out) == 1
    r = out[0]
    assert r.ok and r.src_md5 == CONTENT_MD5 and r.dst_md5 == CONTENT_MD5
    assert r.bad_tasks == ()
    dst.md5_ranges.assert_not_called()


def test_verify_files_mismatch_drills_down_to_bad_chunk() -> None:
    good = FakeTask("/s/f", "/d/f", chunk_index=0, start=0, chunk_size=10)
    bad = FakeTask("/s/f", "/d/f", chunk_index=1, start=10, chunk_size=10)
    stream_good, stream_bad = _md5(b"g" * 10), _md5(b"b" * 10)

    src = Mock(spec=RemoteHasher)
    dst = Mock(spec=RemoteHasher)
    src.md5_full.return_value = CONTENT_MD5
    dst.md5_full.return_value = _md5(b"corrupted")
    dst.md5_ranges.return_value = [stream_good, _md5(b"wrong")]

    results = [FakeResult(good, stream_good), FakeResult(bad, stream_bad)]
    out = verify_files(results, src, dst)
    assert len(out) == 1
    r = out[0]
    assert not r.ok
    assert r.bad_tasks == (bad,)
    dst.md5_ranges.assert_called_once_with("/d/f", [(0, 10), (10, 10)])


def test_verify_files_remote_command_failure() -> None:
    """md5sum failing on dst (empty digest) → not ok, chunks re-checked."""
    task = FakeTask("/s/f", "/d/f", chunk_index=0, start=0, chunk_size=5)
    stream = _md5(b"hello")

    src = Mock(spec=RemoteHasher)
    dst = Mock(spec=RemoteHasher)
    src.md5_full.return_value = stream
    dst.md5_full.return_value = ""  # remote md5sum failed
    dst.md5_ranges.return_value = [stream]  # but chunk bytes are fine

    out = verify_files([FakeResult(task, stream)], src, dst)
    assert len(out) == 1
    assert not out[0].ok
    assert out[0].dst_md5 == ""
    assert out[0].bad_tasks == ()  # chunk digest matched, nothing to retry
