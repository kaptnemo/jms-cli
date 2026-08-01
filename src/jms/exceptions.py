"""Custom exceptions for jms-cli."""


class JMSError(Exception):
    """Base exception for all jms-cli errors."""


class ConfigError(JMSError):
    """Raised when configuration is invalid or missing."""


class AuthError(JMSError):
    """Raised when authentication fails."""


class MFARequired(AuthError):
    """Raised when MFA is required but no OTP secret is configured."""


class APIError(JMSError):
    """Raised when a REST API call fails outside the login flow.

    Covers any non-2xx response other than 401 (which raises AuthError)
    and transport-level failures where no response was received.

    Attributes:
        status_code: HTTP status code of the failed response;
            0 when the request never produced a response.
    """

    def __init__(self, message: str, status_code: int = 0) -> None:
        super().__init__(message)
        self.status_code: int = status_code


class AssetError(JMSError):
    """Raised when asset operations fail."""


class TerminalError(JMSError):
    """Raised when terminal/WebSocket operations fail.

    Attributes:
        exit_code: Remote command exit status when the error is a
            non-zero remote exit; ``None`` otherwise.
        output: Captured command output (stdout/stderr) when the error
            was raised after executing a command; ``""`` otherwise.
    """

    def __init__(
        self, message: str, exit_code: int | None = None, output: str = "",
    ) -> None:
        super().__init__(message)
        self.exit_code: int | None = exit_code
        self.output: str = output


class ConnectionTokenError(TerminalError):
    """Raised when connection-token creation fails."""


class TransferError(JMSError):
    """Raised when file transfer operations fail."""
