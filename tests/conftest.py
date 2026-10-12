import pytest

from folio import credentials


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    # Tests must never read a developer's real credentials.
    monkeypatch.setattr(credentials, "DEFAULT_ENV_FILE", tmp_path / ".env.local")
