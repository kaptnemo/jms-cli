"""JumpServer authentication: Bearer token + form-login session cookie, with MFA/TOTP.

KoKo's WebSocket terminal requires the ``jms_sessionid`` cookie from a
Django form login — an API Bearer token alone is rejected — so login must
perform both steps. REST transport itself lives in ``jms.http``; this
module only orchestrates the dual authentication.
"""

from typing import Callable, Optional

import pyotp
import requests

from jms.config import ServerConfig
from jms.exceptions import APIError, AuthError, MFARequired
from jms.http import HTTP_TIMEOUT, RESTClient
from jms.log import logger


class JMSSession(RESTClient):
    """An authenticated session to one JumpServer instance.

    Holds both the Bearer token (REST API) and the ``jms_sessionid``
    cookie (KoKo WebSocket); neither alone is sufficient. REST transport
    is inherited from :class:`RESTClient`.

    Args:
        server: Server configuration.
        otp_prompt: MFA code input callback, invoked only when no
            ``otp_secret`` is configured and the server demands MFA.
            ``None`` (the default) raises :class:`MFARequired` in that
            situation — the library never hijacks stdin; interactive
            prompts are injected by the CLI layer.
    """

    def __init__(
        self,
        server: ServerConfig,
        otp_prompt: Optional[Callable[[], str]] = None,
    ) -> None:
        super().__init__(server.base_url)
        self.server: ServerConfig = server
        self.otp_prompt: Optional[Callable[[], str]] = otp_prompt
        self._logged_in: bool = False

    @property
    def session_id(self) -> str:
        """Current jms_sessionid cookie value."""
        return self.session.cookies.get("jms_sessionid") or ""

    @property
    def is_authenticated(self) -> bool:
        """Whether a valid session has been established."""
        return self._logged_in and bool(self.session_id)

    def login(self) -> None:
        """Perform the full login: API login for the token + form login for the cookie.

        Raises:
            AuthError: Login failed (including transport-level APIError
                surfacing during the login flow).
            MFARequired: MFA is required but no otp_secret is configured
                and no otp_prompt callback was provided.
        """
        logger.info(
            "Logging in to %s as '%s' ...",
            self.base_url, self.server.username,
        )

        # Step 1: API login for the Bearer token (MFA handled inside).
        # Step 2: form login for the session cookie KoKo needs.
        try:
            self._api_login()
            self._form_login()
        except requests.RequestException as e:
            raise AuthError(f"Login request failed: {e}") from e
        except APIError as e:
            # Everything failing during the login flow is an auth error.
            raise AuthError(str(e)) from e

        self._logged_in = True
        logger.info("Login successful.")

    def _api_login(self) -> None:
        """Authenticate via the API for a Bearer token, handling MFA challenge.

        Raises:
            AuthError: Authentication failed.
            MFARequired: MFA is required but no code can be provided.
        """
        r = self.session.post(
            f"{self.base_url}/api/v1/authentication/auth/",
            json={
                "username": self.server.username,
                "password": self.server.password,
            },
            headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        data = self._json(r, "API login")

        # Field names differ across JumpServer versions; check both.
        if r.status_code == 200 and (
            data.get("code") == "mfa_required"
            or data.get("error") == "mfa_required"
        ):
            r = self._handle_mfa()
            data = self._json(r, "API login after MFA")

        if r.status_code not in (200, 201) or "token" not in data:
            msg = data.get("detail", data.get("msg", str(data)))
            raise AuthError(f"API login failed: {msg}")

        self.bearer_token = data["token"]

    def _handle_mfa(self) -> requests.Response:
        """Complete the MFA challenge and retry the API login.

        Returns:
            The retried login response.

        Raises:
            MFARequired: No otp_secret configured and no otp_prompt
                callback provided (or the callback returned empty).
            AuthError: The MFA challenge was rejected by the server.
        """
        if self.server.otp_secret:
            otp_code = pyotp.TOTP(self.server.otp_secret).now()
        else:
            if self.otp_prompt is None:
                raise MFARequired(
                    "MFA required but no otp_secret configured and no "
                    "otp_prompt callback provided."
                )
            otp_code = self.otp_prompt()
            if not otp_code:
                raise MFARequired(
                    "MFA required but no verification code provided."
                )

        r2 = self.session.post(
            f"{self.base_url}/api/v1/authentication/mfa/challenge/",
            json={"type": "otp", "code": otp_code},
            headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
        if r2.status_code not in (200, 201):
            msg = self._json(r2, "MFA").get("msg", r2.text[:200])
            raise AuthError(f"MFA failed: {msg}")
        # Retry authentication once MFA passed.
        return self.session.post(
            f"{self.base_url}/api/v1/authentication/auth/",
            json={
                "username": self.server.username,
                "password": self.server.password,
            },
            headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )

    def _form_login(self) -> None:
        """Perform the Django form login for the ``jms_sessionid`` cookie.

        Raises:
            AuthError: No session cookie was obtained.
        """
        # GET the login page first to seed the csrf cookie.
        self.session.get(
            f"{self.base_url}/core/auth/login/",
            timeout=HTTP_TIMEOUT,
        )
        self.csrf_token = self.session.cookies.get("jms_csrftoken") or ""

        # POST the login form.
        self.session.post(
            f"{self.base_url}/core/auth/login/",
            data={
                "username": self.server.username,
                "password": self.server.password,
                "csrfmiddlewaretoken": self.csrf_token,
            },
            headers={"Referer": f"{self.base_url}/core/auth/login/"},
            allow_redirects=True,
            timeout=HTTP_TIMEOUT,
        )

        if not self.session_id:
            raise AuthError(
                f"Form login failed for '{self.server.username}': "
                f"no session cookie."
            )

        self.csrf_token = (
            self.session.cookies.get("jms_csrftoken") or self.csrf_token
        )
