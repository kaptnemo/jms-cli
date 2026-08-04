"""MFA detection for JumpServer login responses."""


def is_mfa_required(login_response: dict) -> bool:
    """Return True when the API login response demands an MFA code."""
    return login_response.get("code") == "mfa_required"
