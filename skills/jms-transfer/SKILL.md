---
name: jms-transfer
description: >-
  jms-cli file transfer: `jms sftp` automatic direction detection (upload /
  download / cross-server relay), parallel workers (-j default 4), large-file
  chunking (only above 256MB, FULL/FILES_ONLY policies), SEEK vs split-files
  write strategies, SFTP chroot path translation, md5 verification with
  bad-chunk retry, and the rsync/scp ssh-pipe bridge (-e usage, classic /
  openrsync argument forms, known download hang above 4KB). Use whenever the
  user uploads, downloads, relays files, verifies md5, deals with chroot,
  troubleshoots transfer failures, configures the rsync/scp bridge, or writes
  transfer library calls. CLI first (jms sftp / rsync -e "jms ssh-pipe");
  programmatic use via sftp_transfer / relay_transfer / run_transfer.
compatibility: Python >= 3.10; uv environment (paramiko is a project dependency)
metadata:
  author: codex
  version: "1.0"
  project: jms-cli
---

# jms-transfer — File transfer & rsync bridge

## When to use

Trigger on mentions of sftp, upload/download, transfer, relay, rsync, scp,
ssh-pipe, chroot, md5, verify, chunking, large files, `--no-verify`, or `-j`
parallelism — even without naming the transfer module. Use cases: moving a
file or directory, relaying between two bastion assets, incremental rsync,
or investigating a post-transfer checksum mismatch.

## Prerequisites

- Sessions and connection tokens come from the `jms-auth` skill;
  `md5sum`/`cat` verification and merge commands use the `jms-terminal`
  `execute()` semantics.
- Server alias resolution comes from the `jms-config` skill (`--config` and
  default-server handling).

## Core facts (read first)

1. **Direction auto-detection**: a colon marks a remote spec; one remote side
   = upload/download, both remote = memory-streamed relay (no local disk).
   Remote spec format `asset[@server]:path`.
2. **Chunking**: under `FULL`, files larger than 256MB
   (`DEFAULT_CHUNK_THRESHOLD`) are split with a 16MB minimum chunk
   (`MIN_CHUNK_SIZE`); `FILES_ONLY` never splits. Write strategy `seek`
   (shared dst in r+b at offset) or `split-files` (`.partNN` + SSH `cat`
   merge; remote dst only; download is always seek). Details in
   `references/chunking-and-verify.md`.
3. **Verification**: after transfer, remote `md5sum` is compared with the
   local/source md5; on mismatch, per-chunk
   `dd bs=1M iflag=skip_bytes,count_bytes | md5sum` locates bad chunks and
   retransmits (up to 3 rounds); no identifiable bad chunk means no blind
   retry. Chroot deployments require path translation (`--chroot`).
4. **ssh-pipe known limitation**: rsync **downloads** above 4KB hang (KoKo
   channel half-close issue, see DEVELOP.md section 5) — use `jms sftp` for
   downloads; uploads work fine.

## Workflow

### A. CLI first (user operations)

```bash
uv run jms sftp ./data.tar.gz web@prod:/tmp/          # upload
uv run jms sftp web@prod:/tmp/data.tar.gz ./          # download
uv run jms sftp -j 8 ./big/ web@prod:/data/           # 8 parallel workers
uv run jms sftp -R --skip-hidden ./project/ host:/tmp/project/   # recursive, skip hidden
uv run jms sftp web@prod:/f other@prod:/g             # cross-server relay
uv run jms sftp --no-verify web@prod:/f ./            # skip md5 verification
uv run jms sftp --chroot /tmp ./x host:/x             # chroot=/tmp deployment
uv run jms sftp --split-policy split-files ./big host:/big  # part files merged via SSH
```

rsync bridge (upload direction):

```bash
rsync -avz -e "uv run jms ssh-pipe" ./dir/ web@prod:/data/
```

### B. Direction detection and argument checks

Direction uses `parse_transfer_spec(src, dst)`. Before constructing a
transfer call (or when the user's arguments look suspicious), run the
deterministic check:

```bash
uv run python3 skills/jms-transfer/scripts/check_sftp_spec.py <src> <dst>
```

It prints `upload / download / relay` or an error; exit code 0/1.

### C. Library API (programmatic)

```python
from jms import load_config, sftp_transfer, relay_transfer
from jms.io.transfer import RelaySpec, TransferSpec

# Upload
sftp_transfer(
    load_config().get_server("prod"),
    TransferSpec(
        asset="web", server="prod",
        remote_path="/tmp/data.tar.gz", local_path="./data.tar.gz",
        is_upload=True,
    ),
    n_workers=8, chroot="/tmp", verify=True,
)

# Relay (remote -> remote)
relay_transfer(
    RelaySpec(
        src_asset="a", src_server="s1", src_path="/f",
        dst_asset="b", dst_server="s2", dst_path="/g",
    ),
    n_workers=4,
)
```

For fine control use `run_transfer(files, src_factory, dst_factory,
direction, ...)` with `LocalOpenerFactory`/`SFTPOpenerFactory` (the `IOOpener`
abstraction unifies local/remote/relay I/O).

### D. Chroot and verification

- `translate_remote_path(chroot, sftp_path)`:
  `chroot + '/' + path.lstrip('/')`; `chroot='./'` means HOME (exec cwd);
  `'/'` disables translation.
- `RemoteHasher(terminal, chroot)` runs `md5sum`/`dd|md5sum` over SSH exec;
  `LocalHasher` mirrors the same API. Verification commands, timeouts, and
  bad-chunk retry details are in `references/chunking-and-verify.md`.
- `--no-verify` is only for out-of-band verification or when the verify path
  itself is broken (it bypasses the chunk-level retry safety net).

### E. ssh-pipe bridge

Library entry: `run_bridge(asset_name, server_alias, remote_cmd,
config_path) -> int`; the CLI only parses arguments (classic rsync
`-l <asset> <server> <cmd...>`; openrsync `<asset>@<server> <cmd...>`). The
stdio bridge's stdout carries rsync protocol bytes — all diagnostics go to
stderr, and the library layer must never touch stdout. Details and the known
limitation are in `references/ssh-pipe.md`.

### F. Verification

After touching transfer/verify/bridge code, run:

```bash
uv run pytest tests/test_transfer.py tests/test_verify.py tests/test_ssh_pipe.py \
  tests/test_cli.py -q
```

Real-server SFTP e2e is gated by `JMS_TEST_*` env vars.

## Scope & Boundaries

> - Capabilities, commands, subcommands, flags, parameters, environment
>   variables, endpoints, and file paths **not mentioned in this skill are not
>   part of this skill**. Do not guess or extrapolate them from related tools,
>   documentation memory, or pattern-matching across similar CLIs.
> - If the user asks for functionality outside this skill, stop and ask — do
>   not invent it.
> - The consuming agent is not responsible for completing or improving the
>   skill. Discovering missing functionality is a signal to **ask the user**,
>   not to fill the gap silently.
> - If the skill's instructions appear wrong, contradictory, outdated, or
>   produce errors when executed, stop and consult the user before proceeding.
>   Do not "fix" the skill on the fly, substitute an alternative command, or
>   retry with guessed parameters.
> - Modifying or extending the skill itself requires explicit user permission
>   and should be routed back through `skill-creator`.

This skill covers file transfer only; terminal protocols belong to
`jms-terminal`, auth to `jms-auth`, config to `jms-config`. Use `jms sftp`
for downloads (the ssh-pipe download limitation is not fixed here).
