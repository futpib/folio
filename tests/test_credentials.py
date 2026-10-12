import os
import subprocess
import sys
from pathlib import Path

import pytest

from folio.credentials import CredentialFileError, read_credentials


def test_literal_values_quotes_comments_empty_and_provider_separation(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text("""# Comment
export BOG_USERNAME = 'fixture user' # comment
BOG_PASSWORD = "literal $HOME ${TOKEN} $(command) # password"
BINANCE_API_KEY=fixture-key
WOLT_EMAIL=test@example.com
IBKR_PASSWORD=one
IBKR_PASSWORD= # blank overrides prior value
IBKR_USERNAME
UNRELATED="unfinished
""")
    before = dict(os.environ)
    assert read_credentials("bog") == {
        "username": "fixture user",
        "password": "literal $HOME ${TOKEN} $(command) # password",
    }
    assert read_credentials("binance") == {"api_key": "fixture-key"}
    assert read_credentials("wolt") == {"email": "test@example.com"}
    assert read_credentials("ibkr") == {}
    assert dict(os.environ) == before
    path.write_text(r"""BOG_PASSWORD='p\'a\\ss#word' """ + "\n")
    assert read_credentials("bog") == {"password": "p'a\\ss#word"}
    path.write_text("BOG_PASSWORD=a#b\\c # comment\nBOG_USERNAME=\nBOG_OTP=123456\n")
    assert read_credentials("bog") == {"password": "a#b\\c"}


def test_absent_file_returns_missing_values(tmp_path):
    assert not (tmp_path / ".env.local").exists()
    assert read_credentials("bog") == read_credentials("binance") == {}


@pytest.mark.parametrize(
    "contents", [b'BOG_PASSWORD="private', b"BOG_PASSWORD=private\0", b"\xff", b"x" * 65537]
)
def test_bad_file_fails_without_echoing_values(tmp_path, contents):
    path = tmp_path / ".env.local"
    path.write_bytes(contents)
    with pytest.raises(CredentialFileError) as error:
        read_credentials("bog")
    assert "private" not in str(error.value)


def test_gateway_reader_uses_explicit_file_and_returns_only_ibkr(tmp_path):
    path = tmp_path / ".env.local"
    path.write_text(
        "IBKR_USERNAME=fixture\nIBKR_PASSWORD='a $literal # password'\nBOG_PASSWORD=other\n"
    )
    import folio.credentials

    result = subprocess.run(
        [sys.executable, str(Path(folio.credentials.__file__)), str(path)],
        cwd="/",
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr == ""
    assert result.stdout == '{"username": "fixture", "password": "a $literal # password"}\n'
