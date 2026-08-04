# `jms sftp` direction auto-detection and equivalent library calls

## Rule (jms-transfer skill, core fact 1)

- An argument containing `:` is a remote spec `asset[@server]:path`
  (`parse_transfer_spec` decides by the colon; POSIX local paths have none).
- One remote side = `TransferSpec` (upload/download); both remote =
  `RelaySpec` (memory-streamed relay, no local disk); neither remote raises
  `TransferError`.

## Direction and equivalent library calls

### 1) `jms sftp ./data.tar.gz web@prod:/tmp/`

- Direction: **upload** (dst is remote).
- Equivalent: `sftp_transfer(server, TransferSpec(asset="web", server="prod",
  remote_path="/tmp/", local_path="./data.tar.gz", is_upload=True),
  n_workers=4, verify=True)`.

### 2) `jms sftp web@prod:/tmp/data.tar.gz ./`

- Direction: **download** (src is remote).
- Equivalent: `sftp_transfer(server, TransferSpec(asset="web", server="prod",
  remote_path="/tmp/data.tar.gz", local_path="./", is_upload=False))`.

### 3) `jms sftp a@s1:/f b@s2:/g`

- Direction: **relay** (both remote, cross-server memory streaming).
- Equivalent: `relay_transfer(RelaySpec(src_asset="a", src_server="s1",
  src_path="/f", dst_asset="b", dst_server="s2", dst_path="/g"))`.

## check_sftp_spec.py actual output

```text
$ uv run python3 skills/jms-transfer/scripts/check_sftp_spec.py 'a@s1:/f' 'b@s2:/g'
relay: a@s1:/f -> b@s2:/g
```

Exit code 0.
