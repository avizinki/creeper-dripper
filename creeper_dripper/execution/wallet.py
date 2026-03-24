from __future__ import annotations

import base58
from solders.keypair import Keypair


def load_keypair_from_base58(secret: str) -> Keypair:
    secret = secret.strip()
    if not secret:
        raise RuntimeError("Missing BS58_PRIVATE_KEY")
    return Keypair.from_bytes(bytes(base58.b58decode(secret)))
