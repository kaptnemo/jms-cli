"""Shared test fixtures.

The session cache (``jms.config.session``) persists logged-in state to the
platformdirs config directory; every test here redirects it to a per-test
temp directory so unit tests never read or write the developer's real
``~/.config/jms/session.yaml``.
"""

import pytest


@pytest.fixture(autouse=True)
def _isolate_session_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jms.config.session.session_file_path",
        lambda: tmp_path / "session.yaml",
    )
