"""Post-transfer MD5 verification and chunk-level retry.

After ``execute_transfer()`` finishes, this module:

1. Runs ``md5sum`` on each side via SSH exec (``RemoteHasher.md5_full``)
   to check the full-file digest end-to-end.
2. If full-file MD5 disagrees, it computes per-chunk MD5 on the dst
   via ``dd | md5sum`` and compares against the in-memory MD5s
   each worker collected while streaming.
3. Returns the ``FileTask`` list whose bytes don't match — the caller
   re-runs ``execute_transfer()`` on just those chunks and loops.

The MD5s collected during transfer (``TaskResult.md5``) represent the
bytes the source SFTP actually delivered to us; comparing them against
``dd | md5sum`` on the dst reveals paramiko / KoKo write-path corruption
without requiring extra reads of the src.
"""

from __future__ import annotations

import hashlib
import shlex
from dataclasses import dataclass
from typing import Iterable

from jms.io.transfer.models import FileTask, TaskResult
from jms.transport import AbstractTerminal
from jms.log import logger

# Generous timeout for md5sum of multi-GB files (~30s per GB worst case)
MD5_FULL_TIMEOUT: int = 1800

# Per-chunk md5 over dd is fast; allow a few minutes per chunk just in case
MD5_CHUNK_TIMEOUT: int = 600


@dataclass(frozen=True)
class FileVerifyResult:
    """Per-file verification outcome.

    Attributes:
        src_path: Source path on the src host.
        dst_path: Destination path on the dst host.
        src_md5: ``md5sum`` of src (empty if not computed).
        dst_md5: ``md5sum`` of dst (empty if not computed).
        ok: True iff src_md5 == dst_md5 and both non-empty.
        bad_tasks: Chunks whose dst content does not match the
                   md5 the worker recorded while streaming. Only
                   populated when ``ok`` is False.
    """

    src_path: str
    dst_path: str
    src_md5: str
    dst_md5: str
    ok: bool
    bad_tasks: tuple[FileTask, ...]


class RemoteHasher:
    """Computes file / range MD5 on a remote host via SSH exec.

    Uses ``md5sum`` and ``dd`` from coreutils. The hasher itself
    is stateless — only the underlying terminal is reused
    across calls so we don't pay the connect-token round trip
    each invocation.

    Args:
        terminal: An open ``AbstractTerminal`` bound to the target host.
        chroot: SFTP chroot in SSH-exec terms (see
            ``translate_remote_path``). Defaults to ``/`` (no
            translation — SFTP and SSH share one namespace).
    """

    def __init__(
        self, terminal: AbstractTerminal, chroot: str = "/",
    ) -> None:
        self._term: AbstractTerminal = terminal
        self._chroot: str = chroot

    def _real(self, sftp_path: str) -> str:
        return translate_remote_path(self._chroot, sftp_path)

    def md5_full(self, path: str) -> str:
        """Compute md5sum of an entire file.

        Returns:
            The hex digest (lowercase 32 chars), or empty string if
            md5sum failed.
        """
        cmd = f"md5sum {shlex.quote(self._real(path))}"
        out = self._term.execute(cmd, timeout=MD5_FULL_TIMEOUT)
        return _parse_md5_line(out)

    def md5_ranges(
        self,
        path: str,
        ranges: list[tuple[int, int]],
    ) -> list[str]:
        """Compute md5 of multiple byte ranges in one round trip.

        Args:
            path: Remote file path.
            ranges: ``[(start, length), ...]`` byte ranges. Ranges
                    may be in any order but each is independent.

        Returns:
            Parallel list of hex digests, one per input range.
            An entry is empty string if that range failed.
        """
        if not ranges:
            return []

        # Build one shell pipeline per range; chain with ``;`` so a
        # single bad range doesn't abort the rest. dd's
        # ``iflag=skip_bytes,count_bytes`` lets us pass byte offsets
        # directly without a small bs= unit (so it's fast).
        parts: list[str] = []
        q = shlex.quote(self._real(path))
        for start, length in ranges:
            parts.append(
                f"dd if={q} bs=1M iflag=skip_bytes,count_bytes "
                f"skip={start} count={length} status=none 2>/dev/null "
                f"| md5sum"
            )
        cmd = "; ".join(parts)

        # Timeout scales with number of ranges
        out = self._term.execute(
            cmd, timeout=max(MD5_CHUNK_TIMEOUT, 30 * len(ranges)),
        )

        digests: list[str] = []
        for line in out.splitlines():
            d = _parse_md5_line(line)
            if d:
                digests.append(d)
        # Pad with empties if the remote produced fewer lines than
        # expected (e.g. partial truncation).
        while len(digests) < len(ranges):
            digests.append("")
        return digests[: len(ranges)]


def _parse_md5_line(line: str) -> str:
    """Extract a 32-char hex digest from one ``md5sum`` output line."""
    line = line.strip()
    if not line:
        return ""
    head = line.split(None, 1)[0].lower()
    if len(head) == 32 and all(c in "0123456789abcdef" for c in head):
        return head
    return ""


def translate_remote_path(chroot: str, sftp_path: str) -> str:
    """Translate an SFTP-side path to the real (SSH-exec-side) path.

    Some JumpServer / KoKo deployments expose SFTP via a chroot that
    differs from the SSH-exec view of the filesystem. For example:

    - HOME chroot: SFTP ``/foo`` lives on disk at ``$HOME/foo``;
      pass ``chroot='./'`` so the SSH-exec md5sum runs against
      ``./foo`` (= ``$HOME/foo`` since exec cwd is HOME).
    - ``/tmp`` chroot: SFTP ``/foo`` lives on disk at ``/tmp/foo``;
      pass ``chroot='/tmp'``.

    The translation rule is ``chroot + '/' + sftp_path.lstrip('/')``
    with light normalization. A relative ``sftp_path`` (no leading
    ``/``) is also treated as relative to the chroot.

    Args:
        chroot: SFTP chroot location in SSH-exec terms. ``./`` and
                ``.`` both denote HOME (exec cwd). Use ``/`` to disable
                translation entirely (i.e. SFTP path == SSH path).
        sftp_path: Path as the SFTP client sees it.

    Returns:
        The path to use when invoking ``md5sum`` / ``cat`` / etc.
        via the SSH exec channel.
    """
    if not chroot or chroot == "/":
        return sftp_path
    # Keep the "./" form so SSH-exec resolves relative to its cwd (HOME).
    if chroot in (".", "./"):
        return "./" + sftp_path.lstrip("/")
    return chroot.rstrip("/") + "/" + sftp_path.lstrip("/")


class LocalHasher:
    """In-process MD5 hasher for local files (mirrors ``RemoteHasher`` API).

    Used on the local side of upload/download transfers so the
    verification code path stays uniform regardless of where the
    file lives.
    """

    _READ_CHUNK: int = 4 * 1024 * 1024  # 4 MiB

    def md5_full(self, path: str) -> str:
        """Compute md5 of an entire local file; empty string on error."""
        try:
            h = hashlib.md5()
            with open(path, "rb") as f:
                while True:
                    data = f.read(self._READ_CHUNK)
                    if not data:
                        break
                    h.update(data)
            return h.hexdigest()
        except OSError as e:
            logger.warning("local md5_full failed for %s: %s", path, e)
            return ""

    def md5_ranges(
        self,
        path: str,
        ranges: list[tuple[int, int]],
    ) -> list[str]:
        """Compute md5 of byte ranges of a local file (see RemoteHasher)."""
        out: list[str] = []
        try:
            with open(path, "rb") as f:
                for start, length in ranges:
                    h = hashlib.md5()
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        data = f.read(min(self._READ_CHUNK, remaining))
                        if not data:
                            break
                        h.update(data)
                        remaining -= len(data)
                    out.append(h.hexdigest() if length > 0 else "")
        except OSError as e:
            logger.warning("local md5_ranges failed for %s: %s", path, e)
            while len(out) < len(ranges):
                out.append("")
        return out


# ──── Orchestration helpers ─────────────────────────────────────


def group_tasks_by_file(
    tasks: Iterable[FileTask],
) -> dict[tuple[str, str], list[FileTask]]:
    """Group chunks by their ``(src_path, effective_dst)`` file pair.

    ``effective_dst`` is ``task.merge_to`` if the task is a part of
    a SPLIT_FILES merge, otherwise ``task.dst_path``. This makes
    the post-merge logical file the natural unit of verification
    while still letting the caller retransmit individual ``.partNN``
    files (the bad ``FileTask`` objects retain their original
    ``dst_path``).

    Returns the groups in insertion order so caller logs stay stable.
    """
    groups: dict[tuple[str, str], list[FileTask]] = {}
    for t in tasks:
        effective_dst = t.merge_to or t.dst_path
        groups.setdefault((t.src_path, effective_dst), []).append(t)
    # Sort each group by chunk_index for deterministic md5_ranges order
    for v in groups.values():
        v.sort(key=lambda x: x.chunk_index)
    return groups


def verify_files(
    results: list[TaskResult],
    src_hasher: RemoteHasher,
    dst_hasher: RemoteHasher,
) -> list[FileVerifyResult]:
    """Run full-file md5sum on each transferred file.

    If full md5 mismatches, drill down to per-chunk md5 on the dst
    and compare against the in-memory ``TaskResult.md5`` (what the
    worker actually streamed). Chunks whose dst content differs from
    the streamed md5 are returned in ``bad_tasks`` for retransmission.

    Args:
        results: Output of ``execute_transfer()``.
        src_hasher: Hasher bound to the src host.
        dst_hasher: Hasher bound to the dst host.

    Returns:
        One ``FileVerifyResult`` per distinct file.
    """
    md5_by_task: dict[int, str] = {id(r.task): r.md5 for r in results}
    groups = group_tasks_by_file(r.task for r in results)

    out: list[FileVerifyResult] = []
    for (src_path, dst_path), file_tasks in groups.items():
        src_md5 = src_hasher.md5_full(src_path)
        dst_md5 = dst_hasher.md5_full(dst_path)
        logger.info(
            "verify: src=%s (md5=%s) dst=%s (md5=%s)",
            src_path, src_md5 or "?",
            dst_path, dst_md5 or "?",
        )
        if src_md5 and dst_md5 and src_md5 == dst_md5:
            out.append(FileVerifyResult(
                src_path=src_path, dst_path=dst_path,
                src_md5=src_md5, dst_md5=dst_md5,
                ok=True, bad_tasks=(),
            ))
            continue

        # Drill down: per-chunk md5 on dst
        ranges = [(t.start, t.chunk_size) for t in file_tasks]
        dst_chunk_md5s = dst_hasher.md5_ranges(dst_path, ranges)
        bad: list[FileTask] = []
        for t, dst_chunk_md5 in zip(file_tasks, dst_chunk_md5s):
            stream_md5 = md5_by_task.get(id(t), "")
            if not stream_md5:
                continue
            if not dst_chunk_md5 or dst_chunk_md5 != stream_md5:
                logger.warning(
                    "chunk %d of %s mismatch: "
                    "stream=%s dst=%s (range [%d, %d))",
                    t.chunk_index, dst_path,
                    stream_md5, dst_chunk_md5 or "?",
                    t.start, t.end,
                )
                bad.append(t)

        out.append(FileVerifyResult(
            src_path=src_path, dst_path=dst_path,
            src_md5=src_md5, dst_md5=dst_md5,
            ok=False, bad_tasks=tuple(bad),
        ))

    return out
