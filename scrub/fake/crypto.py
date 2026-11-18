"""Passphrase encryption for the mapping file (SPEC §5.2): scrypt -> Fernet."""
from __future__ import annotations

import base64
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MIN_PASSPHRASE = 12
_N, _R, _P = 2**15, 8, 1


class BadPassphrase(Exception):
    pass


def _key(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=n, r=r, p=p)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def check_passphrase(passphrase: str) -> None:
    if not passphrase or len(passphrase) < MIN_PASSPHRASE:
        raise ValueError(f"Passphrase must be at least {MIN_PASSPHRASE} characters")


def encrypt_json(obj: dict, passphrase: str) -> bytes:
    check_passphrase(passphrase)
    salt = os.urandom(16)
    token = Fernet(_key(passphrase, salt, _N, _R, _P)).encrypt(json.dumps(obj).encode("utf-8"))
    envelope = {
        "format": "entity-scrub-mapping",
        "v": 1,
        "kdf": {"name": "scrypt", "salt": base64.b64encode(salt).decode(), "n": _N, "r": _R, "p": _P},
        "token": token.decode(),
    }
    return json.dumps(envelope, indent=1).encode("utf-8")


def decrypt_json(data: bytes, passphrase: str) -> dict:
    try:
        env = json.loads(data)
        kdf = env["kdf"]
        key = _key(passphrase, base64.b64decode(kdf["salt"]), kdf["n"], kdf["r"], kdf["p"])
        plain = Fernet(key).decrypt(env["token"].encode())
    except InvalidToken as e:
        raise BadPassphrase("Wrong passphrase, or the mapping file is damaged") from e
    except (KeyError, ValueError, TypeError) as e:
        raise BadPassphrase("Not an entity-scrub mapping file") from e
    return json.loads(plain)
