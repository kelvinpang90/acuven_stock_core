"""后台管理员的用户名与密码规则、密码哈希（docs/ADMIN_AUTH.md 4.1）。

不碰数据库。哈希用标准库 hashlib.scrypt，要加载 _hashlib，所以测试在
tests/integration/test_pw_hash.py（不需要数据库）。密码与哈希不进日志与异常消息。
"""

import base64
import hashlib
import hmac
import os
import re

# 整串匹配：re.match 会放过结尾的换行。
_USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

SCHEME = "scrypt"
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 5
SALT_BYTES = 16
DIGEST_BYTES = 64
# 校验时接受的参数上限：N 不超过 2^16，r、p 不超过 16。
MAX_N = 2**16
MAX_R = 16
MAX_P = 16
# 显式给出，接受的参数上限也能算（128 × r × N 再留 1 MiB）。
MAXMEM = 128 * MAX_R * MAX_N + 2**20

_PARAMS = re.compile(r"n=([0-9]{1,6}),r=([0-9]{1,2}),p=([0-9]{1,2})")
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+")


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes | None:
    if not _BASE64URL.fullmatch(text):
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        # 含 binascii.Error：长度不对（除 4 余 1）。
        return None


def _format(n: int, r: int, p: int, salt: bytes, digest: bytes) -> str:
    return f"{SCHEME}$n={n},r={r},p={p}${_encode(salt)}${_encode(digest)}"


# 盐与摘要全零字节：对任何密码都不通过。用户名不存在时照样对它校验一次，两种失败耗时相近。
DUMMY_PASSWORD_HASH = _format(SCRYPT_N, SCRYPT_R, SCRYPT_P, bytes(SALT_BYTES), bytes(DIGEST_BYTES))


def normalize_username(text: str) -> str:
    """规范用户名：str.lower()。合法与否另由 is_valid_username 判断。"""
    return text.lower()


def is_valid_username(name: object) -> bool:
    """3–32 个 ASCII 字符：小写字母、数字、.、_、-，首字符是字母或数字。"""
    return isinstance(name, str) and _USERNAME.fullmatch(name) is not None


def _utf8(password: str) -> bytes | None:
    try:
        return password.encode("utf-8")
    except UnicodeEncodeError:
        return None


def is_valid_password(password: object, *, username: str) -> bool:
    """12–128 个字符（按码点计）、能按 UTF-8 编码、转小写后不等于规范用户名。"""
    if not isinstance(password, str):
        return False
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        return False
    if _utf8(password) is None:
        return False
    return password.lower() != username


def hash_password(password: str) -> str:
    """随机盐的 scrypt 哈希，存成自描述字符串；密码不能按 UTF-8 编码时报 ValueError。"""
    raw = _utf8(password) if isinstance(password, str) else None
    if raw is None:
        # 不带原文：UnicodeEncodeError 的对象里存着整个密码。
        raise ValueError("password must be a UTF-8 encodable string")
    salt = os.urandom(SALT_BYTES)
    digest = hashlib.scrypt(
        raw, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=MAXMEM, dklen=DIGEST_BYTES
    )
    return _format(SCRYPT_N, SCRYPT_R, SCRYPT_P, salt, digest)


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    parts = stored.split("$")
    if len(parts) != 4 or parts[0] != SCHEME:
        return None
    params = _PARAMS.fullmatch(parts[1])
    if params is None:
        return None
    n, r, p = (int(value) for value in params.groups())
    if n <= 1 or n & (n - 1) or n > MAX_N:
        return None
    if not 1 <= r <= MAX_R or not 1 <= p <= MAX_P:
        return None
    salt, digest = _decode(parts[2]), _decode(parts[3])
    if salt is None or len(salt) != SALT_BYTES or digest is None or len(digest) != DIGEST_BYTES:
        return None
    return n, r, p, salt, digest


def verify_password(password: object, stored: object) -> bool:
    """按串里记下的参数重算并比较；任何不合格或计算失败都返回 False，不抛异常。"""
    if not isinstance(password, str) or not isinstance(stored, str):
        return False
    raw = _utf8(password)
    parsed = _parse(stored)
    if raw is None or parsed is None:
        return False
    n, r, p, salt, digest = parsed
    try:
        computed = hashlib.scrypt(raw, salt=salt, n=n, r=r, p=p, maxmem=MAXMEM, dklen=len(digest))
    except (ValueError, MemoryError, OverflowError):
        return False
    return hmac.compare_digest(computed, digest)
