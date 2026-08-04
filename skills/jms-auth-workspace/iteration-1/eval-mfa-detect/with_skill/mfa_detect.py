"""MFA detection for JumpServer login responses (jms-auth skill)."""


def is_mfa_required(login_response: dict) -> bool:
    """Return True when the API login response demands an MFA code.

    JumpServer versions disagree on the field name: some return
    ``{"code": "mfa_required"}``, others ``{"error": "mfa_required"}``
    (both can appear on HTTP 200). The library checks BOTH fields
    (src/jms/core/auth.py) — mirror that here.

    When MFA is required and ``server.otp_secret`` is configured, the
    library computes the code automatically with
    ``pyotp.TOTP(otp_secret).now()``; otherwise an injected prompt
    callback is used (no network needed).
    """
    return (
        login_response.get("code") == "mfa_required"
        or login_response.get("error") == "mfa_required"
    )
