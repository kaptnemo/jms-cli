"""REST transport for the JumpServer API.

Owns the shared ``requests.Session`` (urllib3 retry included), JSON
parsing, error classification and limit/offset pagination. Login and MFA
orchestration live in ``jms.auth``; this module only moves bytes.

Error classification: 401 raises :class:`AuthError` (bad/expired token),
any other non-2xx raises :class:`APIError`, and transport failures raise
:class:`APIError` with ``status_code == 0``.
"""

from typing import Any, Iterator, Optional

import requests

from jms.exceptions import APIError, AuthError

# HTTP request timeout in seconds.
HTTP_TIMEOUT: int = 15


class RESTClient:
    """Authenticated REST transport for one JumpServer instance.

    Holds the shared session plus the credential material the API expects
    on every call (Bearer token, CSRF token). Subclassed by
    ``jms.core.auth.JMSSession``, which fills the tokens in during login.

    Args:
        base_url: Base URL of the JumpServer instance (no trailing slash).
    """

    def __init__(self, base_url: str) -> None:
        self.base_url: str = base_url
        self.session: requests.Session = self._build_session()
        self.csrf_token: str = ""
        self.bearer_token: str = ""

    @staticmethod
    def _build_session() -> requests.Session:
        """Create a requests Session with transport-level retries.

        Retries connection errors/resets/timeouts and 502/503/504 for all
        methods (POST included), tolerating high-latency or flaky links.
        """
        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter

        retry = Retry(
            total=3,
            backoff_factor=0.5,          # 0s, 0.5s, 1s
            status_forcelist=[502, 503, 504],
            allowed_methods=None,        # retry every method, POST included
            raise_on_status=False,       # status codes are checked by the caller
        )
        adapter = HTTPAdapter(max_retries=retry)

        sess = requests.Session()
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        return sess

    @staticmethod
    def _json(r: requests.Response, context: str) -> Any:
        """Parse a response body as JSON.

        Args:
            r: The response object.
            context: Short description of the call, used in error messages.

        Returns:
            The decoded JSON payload.

        Raises:
            APIError: The body is not valid JSON (e.g. an nginx error page).
        """
        try:
            return r.json()
        except ValueError as e:
            raise APIError(
                f"{context}: invalid JSON response "
                f"(HTTP {r.status_code}): {r.text[:200]}",
                status_code=r.status_code,
            ) from e

    @property
    def _api_headers(self) -> dict[str, str]:
        """Request headers carrying the Bearer and CSRF tokens."""
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.bearer_token:
            h["Authorization"] = f"Bearer {self.bearer_token}"
        if self.csrf_token:
            h["X-CSRFToken"] = self.csrf_token
        return h

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
    ) -> Any:
        """Send an authenticated API request and parse the JSON response.

        Args:
            method: HTTP method (GET/POST/PATCH/DELETE).
            path: API path (e.g. ``/api/v1/...``).
            params: Query parameters.
            json_data: JSON request body.

        Returns:
            Decoded JSON payload; ``{}`` for empty bodies (e.g. 204).

        Raises:
            AuthError: The server rejected the token (HTTP 401).
            APIError: Any other non-2xx response, an invalid JSON body,
                or a transport failure (``status_code == 0``).
        """
        try:
            r = self.session.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=json_data,
                headers=self._api_headers,
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException as e:
            raise APIError(
                f"API {method} {path} failed: {e}", status_code=0
            ) from e
        if r.status_code == 401:
            raise AuthError(
                f"API {method} {path} unauthorized (HTTP 401): {r.text[:200]}"
            )
        if not 200 <= r.status_code < 300:
            raise APIError(
                f"API {method} {path} failed (HTTP {r.status_code}): {r.text[:200]}",
                status_code=r.status_code,
            )
        if r.status_code == 204 or not r.text:
            return {}
        return self._json(r, f"API {method} {path}")

    def api_get(self, path: str, params: Optional[dict] = None) -> Any:
        """Send an authenticated GET request.

        Args:
            path: API path.
            params: Query parameters.

        Returns:
            Decoded JSON payload.
        """
        return self._request("GET", path, params=params)

    def api_post(self, path: str, json_data: dict) -> Any:
        """Send an authenticated POST request with a JSON body.

        Args:
            path: API path.
            json_data: Request body.

        Returns:
            Decoded JSON payload.
        """
        return self._request("POST", path, json_data=json_data)

    def api_patch(self, path: str, json_data: dict) -> Any:
        """Send an authenticated PATCH request with a JSON body.

        Args:
            path: API path.
            json_data: Request body.

        Returns:
            Decoded JSON payload.
        """
        return self._request("PATCH", path, json_data=json_data)

    def api_delete(self, path: str) -> Any:
        """Send an authenticated DELETE request.

        Args:
            path: API path.

        Returns:
            Decoded JSON payload; ``{}`` for the usual 204 No Content.
        """
        return self._request("DELETE", path)

    def api_get_all(
        self,
        path: str,
        params: Optional[dict] = None,
        page_size: int = 100,
    ) -> Iterator[dict]:
        """Iterate over every item of a paginated list endpoint.

        JumpServer paginates with ``limit``/``offset`` and answers
        ``{"count": N, "results": [...]}``. Pages are fetched lazily until
        ``count`` items have been yielded. A bare-list response
        (pagination disabled) is yielded as-is.

        Args:
            path: API path of the list endpoint.
            params: Extra query parameters (e.g. ``search``); limit/offset
                are managed by the iterator.
            page_size: Items requested per page.

        Yields:
            One item dict at a time, across all pages.
        """
        query = dict(params or {})
        offset = 0
        while True:
            # Fresh dict per page: callers/mocks may retain the reference.
            page_params = {**query, "limit": page_size, "offset": offset}
            data = self.api_get(path, params=page_params)
            if not isinstance(data, dict):
                yield from data
                return
            results = data.get("results", [])
            if not results:
                return
            yield from results
            offset += len(results)
            if offset >= data.get("count", offset):
                return
