# md5 verification under chroot=/tmp (jms-transfer skill)

## Path translation

`translate_remote_path('/tmp', '/data/x.bin')` =
`chroot.rstrip('/') + '/' + sftp_path.lstrip('/')` = **`/tmp/data/x.bin`**.
(The SFTP-side `/data/x.bin` lives at `/tmp/data/x.bin` in the SSH-exec view.)

## Full-file md5sum (RemoteHasher over SSH exec)

```bash
md5sum /tmp/data/x.bin
```

The real command is assembled with `shlex.quote(translate_remote_path(chroot,
path))`, so paths with special characters are quoted (none here).

## Locating bad chunks on full-file mismatch

```bash
dd if=/tmp/data/x.bin bs=1M iflag=skip_bytes,count_bytes skip=<start> count=<len> status=none 2>/dev/null | md5sum
```

Compared with the per-worker `TaskResult.md5` (the digest of the bytes the
source SFTP actually delivered); only bad chunks are retransmitted (max 3
rounds). **The source is not re-read**: workers already recorded the md5 in
memory while streaming.
