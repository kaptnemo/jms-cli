# Chunking and verification details

## Chunk parameters (`jms/io/transfer/plan.py`)

| Constant | Value | Meaning |
|---|---|---|
| `MIN_CHUNK_SIZE` | 16 MiB | chunk floor, no finer splits |
| `DEFAULT_CHUNK_THRESHOLD` | 256 MiB | split above this (`FULL` + `n_workers>1`) |
| `SFTP_BUFFER_SIZE` | 64 KiB | read/write buffer (paramiko default is 8KB) |
| `SPOT_CHECK_SIZE` | 32 B | post-write sample probes (start/middle/end) |
| `SPOT_CHECK_MAX_ATTEMPTS` | 3 | inline spot-check retries per chunk |

`ChunkPolicy.FULL` (default) = multi-file concurrency + large-file chunking;
`FILES_ONLY` = concurrency without splitting.
`ChunkSplitPolicy.SEEK` (default) = workers share the dst in `r+b` and seek to
`write_offset`; `SPLIT_FILES` = each chunk writes `<dst>.partNN`
(`write_offset=0`) and an SSH `cat` merge assembles the final dst
(`merge_parts_via_ssh`). SPLIT_FILES applies only to remote-dst writes;
download (local pwrite) is always SEEK.

## Merge command (split-files)

```bash
cat <part1> <part2> ... > <target> && stat -c %s <target> && echo __MERGE_OK__
```

The merge verifies the byte count (sum of part sizes); mismatch raises
`TransferError`.

## Verification flow (`jms/io/verify.py`)

1. After transfer, full-file `md5sum` on each side: source
   `md5sum <src>` (`RemoteHasher.md5_full` or local `LocalHasher`).
2. On mismatch, per-chunk:
   `dd if=<path> bs=1M iflag=skip_bytes,count_bytes skip=<start> count=<len>
   status=none 2>/dev/null | md5sum`, compared against the worker's streamed
   `TaskResult.md5` (no need to re-read the source).
3. Returns `FileVerifyResult.bad_tasks`; the caller retransmits only bad
   chunks, up to `max_retries=3` rounds; when no chunk can be blamed, it
   raises `TransferError: md5 mismatch but no chunk identified as corrupt ...
   Refusing to retry blindly.`

Timeouts: `MD5_FULL_TIMEOUT=1800s`; per-chunk `max(600, 30*n)` seconds.

## Path translation

`translate_remote_path(chroot, sftp_path)`:

| chroot | SFTP path | SSH-exec path |
|---|---|---|
| `/` (default, translation off) | `/data/x` | `/data/x` |
| `./` or `.` (HOME) | `/data/x` | `./data/x` |
| `/tmp` | `/data/x` | `/tmp/data/x` |

`--chroot` applies to both sides of a relay. `md5sum`/`cat`/`stat` commands
must use the translated path.

## Engine notes

- All workers on one side share a single `paramiko.Transport` (one connection
  token); each worker opens its own `SFTPClient` channel from that transport
  (Transport is thread-safe, individual SFTP channels are not).
- Large files are pre-allocated at the destination, then seek-written in
  `r+b`; local-dst (download) POSIX pwrite is always safe.
