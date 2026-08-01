"""Tests for jms.transfer — pure-logic layer with behavioral fakes.

The FakeRemoteFS family simulates an SFTP server in memory (files as
bytearrays, implicit directories), driving both the wrapper-level API
(``stat``/``ls`` dicts) and the channel-level API (``open``/``stat``/
``mkdir``) without any network. No mock call-count assertions: tests
verify transferred bytes and observable side effects only.
"""

import hashlib
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

from jms import verify
from jms.exceptions import TerminalError, TransferError
from jms.transfer import (
    ChunkPolicy,
    ChunkSplitPolicy,
    FileInfo,
    LocalOpener,
    LocalOpenerFactory,
    RelaySpec,
    SFTPChannelOpener,
    SFTPOpenerFactory,
    TaskResult,
    TransferSpec,
    connect_sftp,
    execute_transfer,
    group_parts_by_merge_target,
    list_local_files,
    list_remote_files,
    parse_transfer_spec,
    plan_transfer,
    resolve_local_dst,
    resolve_remote_dst,
)


def _bytes(n: int) -> bytes:
    """Deterministic pseudo-random content of length n."""
    return bytes((i * 7 + 13) % 256 for i in range(n))


# ──── Behavioral fake SFTP server ────────────────────────────────


class FakeRemoteFS:
    """In-memory stand-in for an SFTP server.

    Files live in ``files`` (path -> bytearray); directories are
    implicit (any prefix of a file path) plus anything mkdir'ed.
    ``read_hook`` lets a test corrupt (or observe) bytes on read.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytearray] = {}
        self.dirs: set[str] = set()
        self.read_hook: Callable[[str, bytes], bytes] | None = None

    def add_file(self, path: str, data: bytes) -> None:
        self.files[path] = bytearray(data)

    def content(self, path: str) -> bytes:
        return bytes(self.files[path])

    # wrapper-level API (mirrors jms.transfer.SFTPClient) -----------

    def new_channel(self) -> "FakeSFTPChannel":
        return FakeSFTPChannel(self)

    def stat(self, path: str) -> dict:
        if self._is_dir(path):
            return {"size": 0, "is_dir": True}
        if path in self.files:
            return {"size": len(self.files[path]), "is_dir": False}
        raise OSError(f"No such file: {path}")

    def ls(self, path: str = ".") -> list[dict]:
        prefix = path.rstrip("/") + "/"
        entries: dict[str, dict] = {}
        for p in sorted(self.files):
            if not p.startswith(prefix):
                continue
            name = p[len(prefix):].split("/", 1)[0]
            child = prefix + name
            is_dir = self._is_dir(child)
            entries[name] = {
                "name": name,
                "size": 0 if is_dir else len(self.files[child]),
                "is_dir": is_dir,
            }
        return list(entries.values())

    def _is_dir(self, path: str) -> bool:
        if path in self.dirs:
            return True
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self.files)


class FakeSFTPChannel:
    """Channel-level fake covering the paramiko.SFTPClient surface
    that SFTPChannelOpener relies on."""

    def __init__(self, fs: FakeRemoteFS) -> None:
        self._fs = fs
        self.closed = False

    def open(self, path: str, mode: str) -> "FakeRemoteFile":
        if mode == "wb":
            self._fs.files[path] = bytearray()
        elif path not in self._fs.files:
            raise OSError(f"No such file: {path}")
        return FakeRemoteFile(self._fs, path)

    def stat(self, path: str) -> SimpleNamespace:
        if self._fs._is_dir(path):
            return SimpleNamespace(st_size=0, st_mode=stat.S_IFDIR | 0o755)
        if path in self._fs.files:
            return SimpleNamespace(
                st_size=len(self._fs.files[path]),
                st_mode=stat.S_IFREG | 0o644,
            )
        raise OSError(f"No such file: {path}")

    def mkdir(self, path: str) -> None:
        self._fs.dirs.add(path)

    def close(self) -> None:
        self.closed = True


class FakeRemoteFile:
    """File-like object over one FakeRemoteFS bytearray."""

    def __init__(self, fs: FakeRemoteFS, path: str) -> None:
        self._fs = fs
        self._path = path
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        buf = self._fs.files[self._path]
        if size is None or size < 0:
            size = len(buf) - self._pos
        data = bytes(buf[self._pos:self._pos + size])
        self._pos += len(data)
        if self._fs.read_hook is not None:
            data = self._fs.read_hook(self._path, data)
        return data

    def write(self, data: bytes) -> int:
        buf = self._fs.files[self._path]
        end = self._pos + len(data)
        if end > len(buf):
            buf.extend(b"\0" * (end - len(buf)))
        buf[self._pos:end] = data
        self._pos = end
        return len(data)

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        else:
            self._pos = len(self._fs.files[self._path]) + offset
        return self._pos

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def __enter__(self) -> "FakeRemoteFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ──── Direction detection ────────────────────────────────────────


def test_parse_upload() -> None:
    spec = parse_transfer_spec("./report.pdf", "my-host:/tmp/report.pdf")
    assert isinstance(spec, TransferSpec)
    assert spec.is_upload
    assert spec.asset == "my-host"
    assert spec.server is None
    assert spec.remote_path == "/tmp/report.pdf"
    assert spec.local_path == "./report.pdf"


def test_parse_download_with_server() -> None:
    spec = parse_transfer_spec("my-host@prod:/data/out.csv", "./out.csv")
    assert isinstance(spec, TransferSpec)
    assert not spec.is_upload
    assert spec.asset == "my-host"
    assert spec.server == "prod"
    assert spec.local_path == "./out.csv"


def test_parse_relay() -> None:
    spec = parse_transfer_spec("h1@s1:/a.bin", "h2@s2:/b.bin")
    assert isinstance(spec, RelaySpec)
    assert (spec.src_asset, spec.src_server, spec.src_path) == ("h1", "s1", "/a.bin")
    assert (spec.dst_asset, spec.dst_server, spec.dst_path) == ("h2", "s2", "/b.bin")


def test_parse_neither_remote_raises() -> None:
    with pytest.raises(TransferError, match="Neither argument"):
        parse_transfer_spec("./a.txt", "./b.txt")


def test_parse_path_may_contain_at_and_colon() -> None:
    spec = parse_transfer_spec("host:/var/@weird/a:b", "./x")
    assert isinstance(spec, TransferSpec)
    assert spec.remote_path == "/var/@weird/a:b"


def test_parse_trailing_at_means_no_server() -> None:
    spec = parse_transfer_spec("./x", "host@:/p")
    assert isinstance(spec, TransferSpec)
    assert spec.asset == "host"
    assert spec.server is None


def test_parse_last_at_separates_server() -> None:
    spec = parse_transfer_spec("./x", "a@b@c:/p")
    assert isinstance(spec, TransferSpec)
    assert spec.asset == "a@b"
    assert spec.server == "c"


def test_parse_empty_path_raises() -> None:
    with pytest.raises(TransferError, match="remote path is empty"):
        parse_transfer_spec("./x", "host:")


# ──── File listing / recursive expansion ─────────────────────────


def _make_local_tree(root: Path) -> None:
    (root / "a.txt").write_bytes(b"aaa")
    (root / ".hidden").write_bytes(b"h")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_bytes(b"bbbbb")
    (root / "sub" / ".h2").write_bytes(b"x")


def test_list_local_single_file(tmp_path: Path) -> None:
    f = tmp_path / "one.bin"
    f.write_bytes(b"1234")
    files = list_local_files(str(f))
    assert [(x.src_path, x.size) for x in files] == [(str(f), 4)]


def test_list_local_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(TransferError, match="not found"):
        list_local_files(str(tmp_path / "nope"))


def test_list_local_non_recursive(tmp_path: Path) -> None:
    _make_local_tree(tmp_path)
    names = {Path(f.src_path).name for f in list_local_files(str(tmp_path))}
    assert names == {"a.txt", ".hidden"}  # sub/ is a dir, not descended


def test_list_local_recursive_skip_hidden(tmp_path: Path) -> None:
    _make_local_tree(tmp_path)
    all_names = {
        Path(f.src_path).name
        for f in list_local_files(str(tmp_path), recursive=True)
    }
    assert all_names == {"a.txt", ".hidden", "b.txt", ".h2"}

    visible = {
        Path(f.src_path).name
        for f in list_local_files(str(tmp_path), recursive=True, skip_hidden=True)
    }
    assert visible == {"a.txt", "b.txt"}


def test_list_remote_single_file() -> None:
    fs = FakeRemoteFS()
    fs.add_file("/data/a.txt", b"aaa")
    files = list_remote_files(fs, "/data/a.txt")
    assert [(f.src_path, f.size) for f in files] == [("/data/a.txt", 3)]


def test_list_remote_missing_raises() -> None:
    with pytest.raises(TransferError, match="not found"):
        list_remote_files(FakeRemoteFS(), "/nope")


def test_list_remote_recursive_skip_hidden() -> None:
    fs = FakeRemoteFS()
    fs.add_file("/data/a.txt", b"aaa")
    fs.add_file("/data/sub/b.txt", b"bbbbb")
    fs.add_file("/data/.h", b"x")

    flat = list_remote_files(fs, "/data")
    assert {f.src_path for f in flat} == {"/data/a.txt", "/data/.h"}

    deep = list_remote_files(fs, "/data", recursive=True)
    assert {f.src_path for f in deep} == {
        "/data/a.txt", "/data/.h", "/data/sub/b.txt",
    }

    visible = list_remote_files(
        fs, "/data", recursive=True, skip_hidden=True,
    )
    assert {f.src_path for f in visible} == {"/data/a.txt", "/data/sub/b.txt"}


# ──── Transfer planning ──────────────────────────────────────────


def test_plan_small_file_single_task() -> None:
    tasks = plan_transfer([FileInfo("s", "d", 100)])
    assert len(tasks) == 1
    t = tasks[0]
    assert (t.start, t.end, t.chunk_size) == (0, 100, 100)
    assert t.total_chunks == 1 and not t.is_chunked
    assert t.merge_to is None and t.write_offset == 0


def test_plan_chunk_boundaries_seek() -> None:
    tasks = plan_transfer(
        [FileInfo("s", "d", 101)],
        n_workers=4, chunk_threshold=10, min_chunk_size=1,
    )
    assert [(t.start, t.end) for t in tasks] == [
        (0, 25), (25, 50), (50, 75), (75, 100), (100, 101),
    ]
    assert all(t.write_offset == t.start for t in tasks)
    assert all(t.dst_path == "d" and t.merge_to is None for t in tasks)
    assert all(t.total_chunks == 5 and t.is_chunked for t in tasks)


def test_plan_chunk_size_floor() -> None:
    tasks = plan_transfer(
        [FileInfo("s", "d", 100)],
        n_workers=4, chunk_threshold=10, min_chunk_size=40,
    )
    assert [(t.start, t.end) for t in tasks] == [(0, 40), (40, 80), (80, 100)]


def test_plan_split_files_policy() -> None:
    tasks = plan_transfer(
        [FileInfo("s", "/dst/big.bin", 100)],
        n_workers=2, chunk_threshold=10,
        split_policy=ChunkSplitPolicy.SPLIT_FILES, min_chunk_size=1,
    )
    assert [t.dst_path for t in tasks] == [
        "/dst/big.bin.part0000", "/dst/big.bin.part0001",
    ]
    assert all(t.merge_to == "/dst/big.bin" for t in tasks)
    assert all(t.write_offset == 0 for t in tasks)
    assert all(t.is_part_file for t in tasks)


def test_plan_files_only_policy_disables_chunking() -> None:
    tasks = plan_transfer(
        [FileInfo("s", "d", 1000)],
        n_workers=4, chunk_threshold=10, policy=ChunkPolicy.FILES_ONLY,
        min_chunk_size=1,
    )
    assert len(tasks) == 1 and not tasks[0].is_chunked


def test_plan_single_worker_disables_chunking() -> None:
    tasks = plan_transfer(
        [FileInfo("s", "d", 1000)],
        n_workers=1, chunk_threshold=10, min_chunk_size=1,
    )
    assert len(tasks) == 1


def test_verify_contract_grouping() -> None:
    """FileTask/TaskResult fields satisfy the jms.verify contract."""
    tasks = plan_transfer(
        [FileInfo("/a/big.bin", "/b/big.bin", 100)],
        n_workers=2, chunk_threshold=10,
        split_policy=ChunkSplitPolicy.SPLIT_FILES, min_chunk_size=1,
    )
    t = tasks[0]
    assert t.end - t.start == t.chunk_size == 50

    results = [
        TaskResult(task=x, bytes_done=x.chunk_size, md5="d" * 32) for x in tasks
    ]
    groups = verify.group_tasks_by_file(r.task for r in results)
    assert list(groups) == [("/a/big.bin", "/b/big.bin")]

    merged = group_parts_by_merge_target(results)
    assert list(merged) == ["/b/big.bin"]
    assert [r.task.chunk_index for r in merged["/b/big.bin"]] == [0, 1]


# ──── Execution: local chunk offsets ─────────────────────────────


def test_execute_chunked_seek_local(tmp_path: Path) -> None:
    """Chunked SEEK writes land at the right offsets (threaded)."""
    src = tmp_path / "src.bin"
    dst = tmp_path / "dst.bin"
    content = _bytes(100)
    src.write_bytes(content)

    tasks = plan_transfer(
        [FileInfo(str(src), str(dst), len(content))],
        n_workers=2, chunk_threshold=10, min_chunk_size=1,
    )
    assert len(tasks) == 2

    progress: list[int] = []
    results = execute_transfer(
        tasks, LocalOpenerFactory(), LocalOpenerFactory(),
        n_workers=2, callback=lambda t, n: progress.append(n),
    )

    assert dst.read_bytes() == content
    assert [r.task.chunk_index for r in results] == [0, 1]  # order kept
    assert all(r.verified and r.attempts == 1 for r in results)
    assert sum(r.bytes_done for r in results) == len(content)
    assert all(len(r.md5) == 32 for r in results)  # per-chunk md5 recorded
    assert sum(progress) == len(content)


def test_execute_split_files_to_remote() -> None:
    """SPLIT_FILES chunks become .partNN files with exact slices."""
    fs = FakeRemoteFS()
    content = _bytes(100)
    src_fs = FakeRemoteFS()
    src_fs.add_file("/src/big.bin", content)

    tasks = plan_transfer(
        [FileInfo("/src/big.bin", "/dst/big.bin", len(content))],
        n_workers=2, chunk_threshold=10,
        split_policy=ChunkSplitPolicy.SPLIT_FILES, min_chunk_size=1,
    )
    results = execute_transfer(
        tasks, SFTPOpenerFactory(src_fs), SFTPOpenerFactory(fs),
        n_workers=1,
    )

    assert fs.content("/dst/big.bin.part0000") == content[:50]
    assert fs.content("/dst/big.bin.part0001") == content[50:]
    assert all(r.verified for r in results)

    merged = group_parts_by_merge_target(results)
    assert [r.task.chunk_index for r in merged["/dst/big.bin"]] == [0, 1]


# ──── Execution: three IOOpener modes ────────────────────────────


def test_upload_local_to_remote(tmp_path: Path) -> None:
    src = tmp_path / "up.bin"
    content = _bytes(37)
    src.write_bytes(content)
    fs = FakeRemoteFS()

    tasks = plan_transfer([FileInfo(str(src), "/uploads/out.bin", len(content))])
    results = execute_transfer(
        tasks, LocalOpenerFactory(), SFTPOpenerFactory(fs),
        n_workers=1,
    )

    assert fs.content("/uploads/out.bin") == content
    assert "/uploads" in fs.dirs  # mkdir_p created the parent
    assert results[0].bytes_done == len(content)
    assert results[0].md5 == hashlib.md5(content).hexdigest()


def test_upload_empty_file(tmp_path: Path) -> None:
    src = tmp_path / "empty.bin"
    src.write_bytes(b"")
    fs = FakeRemoteFS()

    tasks = plan_transfer([FileInfo(str(src), "/e.bin", 0)])
    results = execute_transfer(
        tasks, LocalOpenerFactory(), SFTPOpenerFactory(fs),
        n_workers=1,
    )

    assert fs.content("/e.bin") == b""
    assert results[0].bytes_done == 0
    assert results[0].md5 == ""


def test_download_remote_to_local(tmp_path: Path) -> None:
    fs = FakeRemoteFS()
    content = _bytes(64)
    fs.add_file("/data/f.bin", content)
    dst = tmp_path / "out.bin"

    tasks = plan_transfer([FileInfo("/data/f.bin", str(dst), len(content))])
    execute_transfer(
        tasks, SFTPOpenerFactory(fs), LocalOpenerFactory(),
        n_workers=1,
    )

    assert dst.read_bytes() == content


def test_relay_streams_remote_to_remote() -> None:
    """Relay: two SFTP factories, streamed via memory, no local disk."""
    src_fs = FakeRemoteFS()
    dst_fs = FakeRemoteFS()
    content = _bytes(100)
    src_fs.add_file("/a/big.bin", content)

    tasks = plan_transfer(
        [FileInfo("/a/big.bin", "/b/big.bin", len(content))],
        n_workers=2, chunk_threshold=10, min_chunk_size=1,
    )
    results = execute_transfer(
        tasks,
        SFTPOpenerFactory(src_fs),
        SFTPOpenerFactory(dst_fs),
        n_workers=2,
    )

    assert dst_fs.content("/b/big.bin") == content
    assert all(r.verified for r in results)


# ──── Spot check retry ───────────────────────────────────────────


def test_spot_check_failure_retries_and_rewinds() -> None:
    """A corrupted read-back triggers one inline retry; the progress
    callback is rewound so deltas still sum to the file size."""
    src_fs = FakeRemoteFS()
    dst_fs = FakeRemoteFS()
    content = _bytes(100)
    src_fs.add_file("/s.bin", content)

    fired = {"n": 0}

    def flaky_read(path: str, data: bytes) -> bytes:
        if path == "/d.bin" and fired["n"] == 0 and data:
            fired["n"] += 1
            return bytes([data[0] ^ 0xFF]) + data[1:]
        return data

    dst_fs.read_hook = flaky_read

    tasks = plan_transfer(
        [FileInfo("/s.bin", "/d.bin", len(content))],
        n_workers=2, chunk_threshold=10, min_chunk_size=1,
    )
    deltas: list[int] = []
    results = execute_transfer(
        tasks,
        SFTPOpenerFactory(src_fs),
        SFTPOpenerFactory(dst_fs),
        n_workers=1, callback=lambda t, n: deltas.append(n),
    )

    assert fired["n"] == 1  # corruption was injected exactly once
    assert results[0].attempts == 2 and results[0].verified
    assert results[1].attempts == 1
    assert dst_fs.content("/d.bin") == content
    assert -50 in deltas  # rewind of the failed first attempt
    assert sum(deltas) == len(content)


# ──── Opener mechanics ───────────────────────────────────────────


def test_local_pre_allocate_idempotent(tmp_path: Path) -> None:
    f = tmp_path / "pre.bin"
    opener = LocalOpener()
    opener.pre_allocate(str(f), 100)
    assert f.stat().st_size == 100

    f.write_bytes(_bytes(100))
    opener.pre_allocate(str(f), 100)  # same size: must not truncate
    assert f.read_bytes() == _bytes(100)


def test_sftp_pre_allocate_and_mkdir_p() -> None:
    fs = FakeRemoteFS()
    opener = SFTPChannelOpener(fs.new_channel())

    opener.mkdir_p("/x/y/out.bin")
    assert "/x" in fs.dirs and "/x/y" in fs.dirs

    opener.pre_allocate("/x/y/out.bin", 50)
    assert len(fs.files["/x/y/out.bin"]) == 50
    assert fs.content("/x/y/out.bin").endswith(b"\0")

    fs.files["/x/y/out.bin"] = bytearray(_bytes(50))
    opener.pre_allocate("/x/y/out.bin", 50)  # idempotent: keep content
    assert fs.content("/x/y/out.bin") == _bytes(50)


# ──── cp-semantics dst resolution ────────────────────────────────


def test_resolve_local_dst(tmp_path: Path) -> None:
    assert resolve_local_dst(str(tmp_path), "f.bin") == str(tmp_path / "f.bin")
    missing = str(tmp_path / "new.bin")
    assert resolve_local_dst(missing, "f.bin") == missing


def test_resolve_remote_dst() -> None:
    fs = FakeRemoteFS()
    fs.add_file("/dir/existing.txt", b"x")
    assert resolve_remote_dst(fs, "/dir", "f.bin") == "/dir/f.bin"
    assert resolve_remote_dst(fs, "/dir/", "f.bin") == "/dir/f.bin"
    assert resolve_remote_dst(fs, "/new.bin", "f.bin") == "/new.bin"
    # existing file: overwritten verbatim (cp semantics)
    assert resolve_remote_dst(fs, "/dir/existing.txt", "f.bin") == "/dir/existing.txt"


# ──── connect_sftp ───────────────────────────────────────────────


class _FakeTransport:
    """Stand-in for paramiko.Transport: only close() matters."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeParamikoSFTP:
    def __init__(self) -> None:
        self.chan = SimpleNamespace(settimeout=lambda t: None)
        self.closed = False

    def get_channel(self) -> SimpleNamespace:
        return self.chan

    def close(self) -> None:
        self.closed = True


def test_connect_sftp_success(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _FakeTransport()
    raw = _FakeParamikoSFTP()
    captured: dict = {}

    def fake_open(session: object, asset: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return transport

    monkeypatch.setattr("jms.transfer.open_koko_transport", fake_open)
    monkeypatch.setattr(
        "jms.transfer.paramiko.SFTPClient.from_transport", lambda t: raw,
    )

    client = connect_sftp(object(), object())
    assert client.transport is transport
    # real-server contract: sftp rides the ssh protocol with web_sftp
    # connect method; protocol="sftp" tokens are rejected by JumpServer
    assert captured == {"protocol": "ssh", "connect_method": "web_sftp"}

    client.close()
    assert raw.closed and transport.closed


def test_connect_sftp_wraps_terminal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(session: object, asset: object, **kwargs: object) -> object:
        raise TerminalError("handshake failed")

    monkeypatch.setattr("jms.transfer.open_koko_transport", boom)
    with pytest.raises(TransferError, match="SFTP connection failed"):
        connect_sftp(object(), object())


def test_connect_sftp_channel_failure_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _FakeTransport()
    monkeypatch.setattr(
        "jms.transfer.open_koko_transport", lambda s, a, **kw: transport,
    )

    def boom(t: object) -> object:
        raise RuntimeError("subsystem rejected")

    monkeypatch.setattr(
        "jms.transfer.paramiko.SFTPClient.from_transport", boom,
    )
    with pytest.raises(TransferError, match="SFTP channel failed"):
        connect_sftp(object(), object())
    assert transport.closed
