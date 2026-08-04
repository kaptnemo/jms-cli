# ssh-pipe bridge protocol and known limitation

## Invocation forms

```bash
rsync -avz -e "uv run jms ssh-pipe" ./dir/ web@prod:/data/
scp -o ProxyCommand="uv run jms ssh-pipe %h" ./f web@prod:/data/
```

rsync calls the bridge in one of two forms:

- classic rsync: `jms ssh-pipe -l <asset> <server> <remote_cmd...>`
- openrsync: `jms ssh-pipe <asset>@<server> <remote_cmd...>`

The CLI parses `(asset, server, remote_cmd)` and calls
`run_bridge(asset, server_alias, remote_cmd, config_path)`.

## Bridge implementation notes

- Library entry: login → `resolve_asset(session, asset, protocol="ssh")` →
  `open_koko_transport()` (SSH 2222, token auth) →
  `chan.exec_command(remote_cmd)`.
- Three relay threads: stdin→channel, channel→stdout, channel→stderr; on
  stdin EOF, `chan.shutdown_write()`.
- **stdout carries rsync protocol bytes only**: all logs/errors go to stderr
  (`jms ssh-pipe: fatal: ...`); exceptions never leak into stdout.
- Returns the remote command's exit status (`chan.recv_exit_status()`).

## Known limitation (do not "fix" casually)

rsync **downloads** above 4KB hang: a KoKo channel half-close issue — the
local rsync half-closes stdin after the file list, and when the bridge
translates EOF into an SSH channel EOF, KoKo tears down the whole channel
(remaining data lost → relay_out blocks forever); not forwarding EOF makes the
remote sender wait and hangs just the same. Version incompatibility and byte
corruption have been ruled out. **Mitigation**: use `jms sftp` for downloads
(md5-verified); rsync for uploads only. A potential fix direction (delayed
EOF: shutdown_write only after relay_out finishes) is unverified, see
`DEVELOP.md` section 5.
