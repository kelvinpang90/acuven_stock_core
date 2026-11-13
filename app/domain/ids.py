"""稳定 ID（契约第 1 节，Q-04）。

格式：类型前缀＋下划线＋26 个字符的 ULID（Crockford Base32，大写）。前 10 个字符是毫秒时间戳，
后 16 个是 80 位随机数，所以较晚生成的 ID 按字符串排序更大。数据库列是 VARCHAR(32)，
前缀只接受小写字母，整个 ID 不超过 32 个字符。调用方只做相等比较，不解析内容。
"""

import os
import re
import time

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LENGTH = 26
MAX_ID_LENGTH = 32
RANDOM_BYTES = 10
_MAX_TIMESTAMP_MS = (1 << 48) - 1
_PREFIX = re.compile(r"[a-z]+")


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(CROCKFORD_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str, *, now_ms: int | None = None, random_bytes: bytes | None = None) -> str:
    """生成 `<prefix>_<ULID>`。

    now_ms（Unix 毫秒）与 random_bytes（10 字节）可注入以便测试；不给时取当前时间与系统随机数。
    """
    if not isinstance(prefix, str) or not _PREFIX.fullmatch(prefix):
        raise ValueError(f"id prefix must be lowercase letters, got {prefix!r}")
    if len(prefix) + 1 + ULID_LENGTH > MAX_ID_LENGTH:
        raise ValueError(f"id prefix {prefix!r} makes the id longer than {MAX_ID_LENGTH}")
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    if random_bytes is None:
        random_bytes = os.urandom(RANDOM_BYTES)
    if isinstance(now_ms, bool) or not isinstance(now_ms, int):
        raise ValueError(f"now_ms must be an integer, got {now_ms!r}")
    if not 0 <= now_ms <= _MAX_TIMESTAMP_MS:
        raise ValueError(f"now_ms out of the 48-bit ULID range: {now_ms}")
    if not isinstance(random_bytes, bytes) or len(random_bytes) != RANDOM_BYTES:
        raise ValueError(f"random_bytes must be {RANDOM_BYTES} bytes")
    randomness = int.from_bytes(random_bytes, "big")
    return f"{prefix}_{_encode(now_ms, 10)}{_encode(randomness, 16)}"
