# jms-cli

[![PyPI version](https://img.shields.io/pypi/v/jms-cli)](https://pypi.org/project/jms-cli/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/jms-cli)](https://pypi.org/project/jms-cli/)
[![CI](https://github.com/GCS-ZHN/jms-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/GCS-ZHN/jms-cli/actions/workflows/ci.yml)

A personal CLI tool and Python library for accessing assets behind a
[JumpServer v4](https://docs.jumpserver.org/zh/v4/) bastion host: remote command
execution, interactive terminal, parallel SFTP file transfer, and rsync/scp
incremental sync via an SSH bridge.

## Features

- `jms exec` — run commands on assets (SSH or WebSocket backend, auto fallback)
- `jms login` — interactive PTY with keepalive (Ctrl+] to exit)
- `jms sftp` — parallel SFTP upload/download, chunked large files, cross-server relay
- `jms ssh-pipe` — `-e` bridge for `rsync` / `scp`
- Multi-server config, MFA (TOTP) support, AES-256-GCM encrypted credentials
- Usable as a Python library (`from jms import JMSSession, connect, ...`)

## Install

```bash
pip install jms-cli     # or: uv tool install jms-cli
```

Requires Python >= 3.10. For development from source:

```bash
git clone git@github.com:GCS-ZHN/jms-cli.git
cd jms-cli && uv sync
```

## Quick start

```bash
jms config add prod              # add a server (interactive, verifies credentials)
jms ls [-q keyword]              # list/search assets
jms exec web@prod uname -a       # run a command (asset[@server])
jms login web@prod               # interactive terminal
jms sftp ./file.tar.gz web@prod:/tmp/   # direction auto-detected, -j N parallel
rsync -avz -e "jms ssh-pipe" ./dir/ web@prod:/data/
```

Config lives in the platform config dir (`platformdirs`, e.g.
`~/Library/Application Support/jms/config.yaml` on macOS, `~/.config/jms/` on
Linux), written with `0600` permissions. Credentials are stored encrypted.
Each command also accepts a hidden `--config <path>` option to point at a
different config file.

## CLI operations

Target syntax is SSH-style: `<asset>[@<server>]`. Omit `@<server>` to use the
default server (the first one you added).

### Server config

```bash
jms config add prod          # interactive; verifies credentials before saving
jms config list              # show servers; default marked with '*'
jms config set-default prod  # change the default server
jms config remove prod       # delete a server (-y to skip confirmation)
```

### List / search assets

```bash
jms ls                       # all assets on the default server
jms ls @prod                 # all assets on server 'prod'
jms ls -q mysql              # filter by keyword
```

### Remote command execution

`-b ssh|ws|auto` selects the backend (auto = SSH first, WebSocket fallback).
The remote exit code is propagated to the local shell.

```bash
jms exec web@prod uname -a
jms exec web ls -la /var/log
jms exec web@prod 'echo hello world'
jms exec -b ws web whoami          # force WebSocket backend
jms exec -t 60 web 'sleep 30'      # longer timeout (default 30s)
```

### Interactive terminal

```bash
jms login web@prod          # Ctrl+] to exit
jms login web               # default server
jms login -b ssh web        # force SSH backend (native PTY)
```

### File transfer (SFTP)

Direction is auto-detected from the arguments; `-j N` sets parallelism.

```bash
jms sftp ./data.tar.gz web@prod:/tmp/        # upload
jms sftp web@prod:/tmp/data.tar.gz ./        # download
jms sftp ./src/ web@prod:~/dst/              # directory tree
jms sftp -j 8 ./big/ web@prod:/data/         # 8 parallel workers
jms sftp --no-verify web@prod:/f ./          # skip md5 verification
jms sftp web@prod:/f other@prod:/g           # cross-server relay (memory-streamed)
```

### rsync / scp bridge

```bash
rsync -avz -e "jms ssh-pipe" ./dir/ web@prod:/data/
scp -o ProxyCommand="jms ssh-pipe %h" ./f web@prod:/data/
```

Known limitation: rsync *downloads* over the bridge may hang on files larger
than ~4KB (see `DEVELOP.md` §5). Use `jms sftp` for downloads; rsync uploads
work fine.

## Library usage

```python
from jms import JMSSession, ServerConfig, resolve_asset, connect, BackendType

server = ServerConfig(name="prod", host="jump.example.com",
                      username="alice", password="...", otp_secret="...")
sess = JMSSession(server)
sess.login()
asset = resolve_asset(sess, "web")
with connect(sess, asset, backend=BackendType.AUTO) as term:
    print(term.execute("uname -a"))
```

## Roadmap / TODO

- [ ] Admin operations (JumpServer management API): asset CRUD, user
      management, permission/grant management — the REST layer (`http.py`) is
      designed for these resource modules
- [x] GitHub Actions CI (pure unit tests always run; real-server tests
      auto-skip without `JMS_TEST_*` env vars)
- [x] PyPI + GitHub release on tag push
- [ ] Dependency audit (`pip-audit`) in CI

## Development

See `DEVELOP.md` (Chinese) for architecture, dev principles and testing rules.
