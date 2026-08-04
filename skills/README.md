# jms-cli Project Skills

Domain-focused Agent Skills for this repository. Each skill covers both
**CLI usage (primary path)** and the **Python SDK / library API**. `SKILL.md`
is the workflow skeleton; long protocol details live in `references/` and load
on demand; deterministic checks use the scripts under `scripts/`.

| Skill | Domain | CLI primary | Library API | Verification |
|---|---|---|---|---|
| `jms-config` | Config & credential crypto | `jms config add/list/remove/set-default` | `load_config` / `add_server` / `remove_server` / `set_default_server` | `scripts/validate_config.py` + `uv run pytest tests/test_config.py tests/test_crypto.py` |
| `jms-auth` | Auth & connection tokens | `jms config add` (credential check) | `JMSSession.login()` / `create_connection_token()` | `uv run pytest tests/test_auth.py tests/test_http.py tests/test_backend_token.py` |
| `jms-terminal` | SSH/WS terminal backends | `jms exec` / `jms login` | `connect()` / `AbstractTerminal` | `uv run pytest tests/test_backend_*.py` |
| `jms-transfer` | File transfer & rsync bridge | `jms sftp` / `rsync -e "jms ssh-pipe"` | `sftp_transfer` / `relay_transfer` / `run_transfer` | `scripts/check_sftp_spec.py` + `uv run pytest tests/test_transfer.py tests/test_verify.py tests/test_ssh_pipe.py` |
| `jms-assets` | Asset discovery & resolution | `jms ls [server] [-q kw] [-n N]` (alias without `@`) | `list_assets` / `search_assets` / `resolve_asset` / `AssetInfo` | `scripts/check_target_spec.py` + `uv run pytest tests/test_assets.py` |

> 2026-08-04 live-server validation: the protocol claims in all five skills
> passed `tests/test_integration.py` 11/11 against a local test JumpServer
> instance. The `jms-auth` SFTP connection-token contract was corrected to
> match the real server: `protocol="ssh"` + `connect_method="web_sftp"`
> (`protocol="sftp"` is rejected with `perm_account_invalid`), consistent
> with `io/transfer/sftp.py` and different from the outdated `token.py`
> docstring. Test credentials are only referenced by env var names
> (`JMS_TEST_*`) — never hard-code local hosts, usernames, passwords, OTP
> secrets, or asset names in skills or this README.

## Installation (user action)

This project keeps skills in `<project>/skills/` as reviewable,
version-controlled source; they are not auto-loaded. To load them, the user
chooses an installation method.

### Via the skills CLI (recommended)

```bash
npx skills add GCS-ZHN/jms-cli
```

This pulls the skills from the GitHub repository and installs them into your
agent's skill directory. Run it from anywhere; the CLI resolves the repo from
GitHub.

### Manually

Copy or symlink `skills/*` into an agent skills directory (such as
`~/.agents/skills/`), or declare this directory in the agent's skill
configuration. This repository does not install skills itself — installation
is the user's step.
