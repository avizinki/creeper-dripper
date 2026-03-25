from __future__ import annotations

import base58
import json
from pathlib import Path

from solders.keypair import Keypair


def load_keypair_from_base58(secret: str) -> Keypair:
    secret = secret.strip()
    if not secret:
        raise RuntimeError("Missing BS58_PRIVATE_KEY")
    return Keypair.from_bytes(bytes(base58.b58decode(secret)))


def load_keypair_from_file(path: Path) -> Keypair:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError("Invalid Solana keypair file format") from exc
    if not isinstance(raw, list) or len(raw) != 64:
        raise RuntimeError("Invalid Solana keypair file format")
    if any((not isinstance(item, int) or item < 0 or item > 255) for item in raw):
        raise RuntimeError("Invalid Solana keypair file format")
    try:
        return Keypair.from_bytes(bytes(raw))
    except Exception as exc:
        raise RuntimeError("Invalid Solana keypair file format") from exc
