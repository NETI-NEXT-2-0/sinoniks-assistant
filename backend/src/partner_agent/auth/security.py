import hashlib
import secrets
import unicodedata

from pwdlib import PasswordHash

password_hasher = PasswordHash.recommended()
# A real hash also makes nonexistent-user failures perform Argon2 verification.
DUMMY_PASSWORD_HASH = password_hasher.hash(secrets.token_urlsafe(32))


def normalize_login(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    if not value or len(value) > 128 or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("login must contain 1–128 non-whitespace characters")
    return value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)
