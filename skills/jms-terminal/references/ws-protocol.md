# KoKo WebSocket terminal protocol (reverse-engineered, do not change)

## Connection

```text
wss://<host>/koko/ws/terminal/?disableautohash=false&token=<token_id>&_=<ms>
```

- Scheme follows HTTP: https→wss, http→ws.
- Headers: `Cookie: jms_sessionid=<sid>; SESSION_COOKIE_NAME_PREFIX=jms_`,
  `Origin: <base_url>`, `Host: <host>`,
  `Sec-WebSocket-Protocol: JMS-KOKO` (subprotocols=["JMS-KOKO"]), 15s connect
  timeout.
- Handshake failure retries once with a fresh connection token
  (`open_ws_terminal`).
- The first frame is the CONNECT message carrying `id` (session UUID); the
  client then sends `TERMINAL_INIT` (200x50) and waits for the shell prompt
  (`wait_for_prompt`: matches `$ `/`# ` suffixes, or treats 3 consecutive
  recv timeouts after data as a custom-prompt idle).

## Message shapes (text frames, JSON)

| type | direction | data field |
|---|---|---|
| `TERMINAL_DATA` | client → server | command string + `\r` (e.g. `"cmd\r"`) |
| `TERMINAL_INIT` | client → server | JSON string of `{"cols": 200, "rows": 50}` |
| `TERMINAL_RESIZE` | client → server | JSON string of `{"cols": N, "rows": N}` |
| `PING` | both | none (heartbeat; reply with `PONG`) |
| `PONG` | both | none |
| `CLOSE` | both | none |

Every message carries `id` (the session UUID). Terminal output is **binary
frames** (opcode 2), not text frames — parsing as text yields garbage/empty.

## The execute marker algorithm

1. Build a unique marker: `__JMSDONE_<epoch_ms>__`.
2. Send: `{cmd}; __rc=$?; echo <marker>; echo __JMSRC:${__rc}__` + `\r`
   (note `${__rc}` — `$__rc__` would expand to empty and break rc capture).
3. Read binary frames until `output.count(marker) >= 2` **and** `__JMSRC:`
   appears. The marker appears twice: once as command echo, once as the echo
   output; the command output is between them.
4. Exit code: `__JMSRC:N__` after the second marker
   (`_RC_RE = rb"__JMSRC:(\d+)__"`).
5. Before returning, `_drain()` (max 2s) clears residual data so the next
   execute is not polluted.

With `check=True`, timeout/connection loss/non-zero rc raise `TerminalError`.

## Known traps

- Commands ending in `#`, `\`, or with unbalanced quotes swallow the rc-capture
  chain and hang until timeout (protocol limitation, not a bug).
- The Nginx WS reverse proxy is a transparent TCP tunnel: **WS protocol-level
  pings (opcode 0x9) never reach KoKo** — heartbeat must be an
  application-level PING text frame (`HEARTBEAT_INTERVAL = 30s`, well under
  KoKo's 5-minute read timeout and Nginx's default 60s
  `proxy_read_timeout`).
- `close()` sends `{"type":"CLOSE"}` before closing the underlying socket.
