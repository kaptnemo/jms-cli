# JumpServer v4 auth endpoints and error semantics

## Endpoint overview

| Endpoint | Method | Purpose | Auth |
|---|---|---|---|
| `/api/v1/authentication/auth/` | POST | API login for the Bearer token; retried after MFA | none (credentials in body) |
| `/api/v1/authentication/mfa/challenge/` | POST | Submit an OTP code to complete the MFA challenge | none |
| `/core/auth/login/` | GET | Seed the csrf cookie (`jms_csrftoken`) | none |
| `/core/auth/login/` | POST | Django form login for `jms_sessionid` | csrfmiddlewaretoken + Referer |
| `/api/v1/authentication/connection-token/` | POST | Create a connection token (SSH/WS/SFTP share this) | Bearer |
| `/api/v1/perms/users/self/assets/` | GET | List/search authorized assets (paginated) | Bearer |
| `/api/v1/perms/users/self/assets/{id}/` | GET | Asset detail (permed_accounts/protocols) | Bearer |

## Request/response shapes

### API login

```json
POST /api/v1/authentication/auth/
{"username": "alice", "password": "..."}
```

Success: `{"token": "<jwt>"}`. MFA-required (may also be HTTP 200):
`{"code": "mfa_required", ...}` or `{"error": "mfa_required", ...}` — both
fields are checked to cover different JumpServer versions.

### MFA challenge

```json
POST /api/v1/authentication/mfa/challenge/
{"type": "otp", "code": "123456"}
```

On 2xx, retry the API login. Non-2xx raises `AuthError: MFA failed: <msg>`.

### Form login

1. `GET /core/auth/login/` seeds the `jms_csrftoken` cookie.
2. `POST /core/auth/login/` with form fields `username`/`password`/
   `csrfmiddlewaretoken`, `Referer: {base_url}/core/auth/login/`,
   `allow_redirects=True`.
3. Require a non-empty `jms_sessionid` cookie, otherwise `AuthError: Form
   login failed ... no session cookie`.

### Connection token

```json
POST /api/v1/authentication/connection-token/
{"asset": "<uuid>", "account": "@USER", "protocol": "ssh", "connect_method": "web_cli"}
```

Response contains `id` and `value`. SSH consumption: port 2222, user
`JMS-{id}`, password `value`; WebSocket consumption: URL query `token={id}` +
`jms_sessionid` cookie.

SFTP token contract (live-server verified, `tests/test_integration.py`):

- Terminals (exec/login): `protocol="ssh"`, `connect_method="web_cli"`.
- SFTP: `protocol="ssh"`, `connect_method="web_sftp"` (reuses the asset's ssh
  protocol with `sftp_enabled`); this version returns `perm_account_invalid`
  for `protocol="sftp"`.
- When the asset has connect-only grants (no upload/download actions), KoKo
  reports "please select one of the assets" for every SFTP path.
- `account` must be an alias (`@USER`, or a named alias from the asset's
  `permed_accounts`), not a display name.

## Error classification (RESTClient)

| Case | Exception | Notes |
|---|---|---|
| HTTP 401 | `AuthError` | token expired/invalid |
| Other non-2xx | `APIError` (`status_code`) | 204 empty body returns `{}` |
| Network/connection failure | `APIError` (`status_code == 0`) | requests.RequestException |
| Response is not JSON | `APIError` | typically an nginx error page |
| Any failure inside the login flow | wrapped as `AuthError` | `JMSSession.login()` |

## Transport behavior

- Each `JMSSession` shares one `requests.Session`: urllib3 `Retry(total=3,
  backoff_factor=0.5, status_forcelist=[502,503,504], allowed_methods=None)` —
  every method (POST included) retries 502/503/504.
- `HTTP_TIMEOUT = 15` seconds (`jms/core/http.py`).
- Pagination: `limit`/`offset`, response `{"count": N, "results": [...]}`;
  `api_get_all()` fetches pages lazily until count is reached; bare arrays are
  yielded as-is.

## Account/protocol selection (assets)

- `select_account`: `@USER` > first non-`@` alias (fallback to username) >
  first account > `@INPUT`.
- `select_protocol`: prefer `ssh`, otherwise the first listed protocol.
- `resolve_asset`: exact-name match first, otherwise the first search result;
  then detail is fetched for `permed_accounts`/`permed_protocols` to build the
  `AssetInfo`.
