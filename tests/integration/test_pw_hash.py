"""用户名与密码规则、scrypt 哈希（docs/ADMIN_AUTH.md 4.1，例子 P1–P4）。

不需要数据库；要加载 _hashlib（Worker 沙箱关闭 Win32k 时加载不了），所以不放 tests/unit。
"""

import base64
import hashlib
import re

import pytest

from app.domain import pw_hash
from app.domain.pw_hash import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    is_valid_password,
    is_valid_username,
    normalize_username,
    verify_password,
)

PASSWORD = "correct horse battery"
STORED = re.compile(r"scrypt\$n=16384,r=8,p=5\$[A-Za-z0-9_-]{22}\$[A-Za-z0-9_-]{86}")


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def stored_with(params: str = "n=16384,r=8,p=5", salt: bytes = b"s" * 16, digest=None) -> str:
    """按给定参数手工拼一个串；digest 不给时按这些参数对 PASSWORD 算出真摘要。"""
    if digest is None:
        digest = hashlib.scrypt(
            PASSWORD.encode(), salt=salt, n=16384, r=8, p=5, maxmem=pw_hash.MAXMEM, dklen=64
        )
    return f"scrypt${params}${b64(salt)}${b64(digest)}"


# --- 用户名 ---


@pytest.mark.parametrize(
    "name",
    ["kelvin", "abc", "warehouse.lead", "a-b_c.d", "0ps", "9" * 32, "a" + "z" * 31],
)
def test_valid_usernames(name):
    assert is_valid_username(name)


@pytest.mark.parametrize(
    "name",
    ["", "ab", "a" * 33, ".kelvin", "_kelvin", "-kelvin", "kel vin", "kelvin!", None, 7],
)
def test_invalid_usernames(name):
    assert not is_valid_username(name)


def test_a_username_ending_in_a_newline_is_invalid():
    # 整串匹配：re.match 会放过结尾的换行。
    assert not is_valid_username("kelvin\n")
    assert not is_valid_username(normalize_username("Kelvin\n"))
    assert not is_valid_username("\nkelvin")


def test_uppercase_is_normalized_to_lowercase_and_only_then_valid():
    assert normalize_username("KELVIN") == "kelvin"
    assert normalize_username("Warehouse.Lead") == "warehouse.lead"
    assert not is_valid_username("KELVIN")
    assert is_valid_username(normalize_username("KELVIN"))


def test_non_ascii_usernames_are_invalid_even_after_normalizing():
    # MySQL 默认排序规则会让 kélvin 匹配到 kelvin；规则层先拒，登录时就不查库。
    for text in ("kélvin", "KÉLVIN", "kelvin ", "ｋｅｌｖｉｎ", "kelvın"):
        assert not is_valid_username(normalize_username(text)), text


# --- 密码规则 ---


def test_p1_password_length_is_12_to_128_characters():
    assert not is_valid_password("a" * 11, username="kelvin")
    assert is_valid_password("a" * 12, username="kelvin")
    assert is_valid_password("a" * 128, username="kelvin")
    assert not is_valid_password("a" * 129, username="kelvin")
    # 按码点计，不按字节。
    assert is_valid_password("密" * 12, username="kelvin")
    assert not is_valid_password("密" * 11, username="kelvin")


def test_p2_password_must_differ_from_the_username_ignoring_case():
    assert len("warehouse.lead") == 14
    assert not is_valid_password("Warehouse.Lead", username="warehouse.lead")
    assert not is_valid_password("WAREHOUSE.LEAD", username="warehouse.lead")
    assert is_valid_password("warehouse.lead2", username="warehouse.lead")


def test_no_composition_rules():
    assert is_valid_password("aaaaaaaaaaaa", username="kelvin")
    assert is_valid_password(" " * 12, username="kelvin")


def test_a_password_with_a_lone_surrogate_is_rejected():
    password = "abcdefghijk\ud800"
    assert len(password) == 12
    assert not is_valid_password(password, username="kelvin")
    with pytest.raises(ValueError) as caught:
        hash_password(password)
    assert password not in str(caught.value)
    # 登录校验返回不通过，不抛异常。
    assert not verify_password(password, hash_password(PASSWORD))
    assert not verify_password(password, DUMMY_PASSWORD_HASH)


def test_non_string_passwords_are_rejected():
    for password in (None, b"correct horse battery", 123456789012):
        assert not is_valid_password(password, username="kelvin")
        assert not verify_password(password, DUMMY_PASSWORD_HASH)


# --- 哈希 ---


def test_p3_hashing_twice_gives_different_strings_that_both_verify():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert first != second
    for stored in (first, second):
        assert STORED.fullmatch(stored)
        assert len(stored) <= 255
        assert verify_password(PASSWORD, stored)
        assert not verify_password(PASSWORD + "x", stored)
        assert not verify_password(PASSWORD.upper(), stored)


def test_the_stored_format_records_salt_and_digest():
    stored = hash_password(PASSWORD)
    _, params, salt, digest = stored.split("$")
    assert params == "n=16384,r=8,p=5"
    raw_salt = base64.urlsafe_b64decode(salt + "==")
    assert len(raw_salt) == 16
    expected = hashlib.scrypt(
        PASSWORD.encode("utf-8"), salt=raw_salt, n=2**14, r=8, p=5, maxmem=pw_hash.MAXMEM, dklen=64
    )
    assert base64.urlsafe_b64decode(digest + "==") == expected


def test_the_password_is_hashed_as_utf8_without_normalization():
    composed, decomposed = "pässwordpässword", "pässwordpässword"
    stored = hash_password(composed)
    assert verify_password(composed, stored)
    assert not verify_password(decomposed, stored)


def test_maxmem_covers_the_accepted_parameter_limits():
    assert pw_hash.MAXMEM == 128 * 16 * 2**16 + 2**20
    assert verify_password(PASSWORD, stored_with())


def test_verification_follows_the_parameters_in_the_string():
    salt = b"t" * 16
    digest = hashlib.scrypt(
        PASSWORD.encode(), salt=salt, n=2**10, r=4, p=1, maxmem=pw_hash.MAXMEM, dklen=64
    )
    assert verify_password(PASSWORD, stored_with("n=1024,r=4,p=1", salt, digest))


def test_p4_dummy_empty_and_malformed_strings_never_verify():
    assert not verify_password(PASSWORD, DUMMY_PASSWORD_HASH)
    assert not verify_password("", DUMMY_PASSWORD_HASH)
    assert not verify_password(PASSWORD, "")
    # N 不是 2 的幂。
    assert not verify_password(PASSWORD, stored_with("n=3,r=8,p=5"))


def test_the_dummy_hash_has_the_standard_parameters_and_zero_bytes():
    assert DUMMY_PASSWORD_HASH == f"scrypt$n=16384,r=8,p=5${b64(bytes(16))}${b64(bytes(64))}"
    assert STORED.fullmatch(DUMMY_PASSWORD_HASH)


@pytest.mark.parametrize(
    "params",
    [
        "n=1,r=8,p=5",
        "n=0,r=8,p=5",
        "n=131072,r=8,p=5",
        "n=16383,r=8,p=5",
        "n=16384,r=17,p=5",
        "n=16384,r=8,p=17",
        "n=16384,r=0,p=5",
        "n=16384,r=8,p=0",
        "n=16384,r=8",
        "N=16384,r=8,p=5",
        "n=16384, r=8, p=5",
        "n=+16384,r=8,p=5",
        "n=１６３８４,r=8,p=5",
    ],
)
def test_parameters_out_of_range_or_malformed_do_not_verify(params):
    assert not verify_password(PASSWORD, stored_with(params))


@pytest.mark.parametrize(
    ("salt", "digest"),
    [(b"s" * 15, None), (b"s" * 17, None), (b"s" * 16, b"d" * 63), (b"s" * 16, b"d" * 65)],
)
def test_wrong_salt_or_digest_lengths_do_not_verify(salt, digest):
    if digest is None:
        digest = b"d" * 64
    assert not verify_password(PASSWORD, stored_with(salt=salt, digest=digest))


@pytest.mark.parametrize(
    "stored",
    [
        "scrypt",
        "scrypt$$$",
        "bcrypt$n=16384,r=8,p=5$AAAA$AAAA",
        "scrypt$n=16384,r=8,p=5$!!!!$????",
        "scrypt$n=16384,r=8,p=5$AAAAA$AAAAA",
        "scrypt$n=16384,r=8,p=5$" + "A" * 22 + "$" + "A" * 86 + "$extra",
        "scrypt$n=16384,r=8,p=5$" + "A" * 22 + "=$" + "A" * 86,
        None,
        12345,
    ],
)
def test_malformed_strings_do_not_verify(stored):
    assert not verify_password(PASSWORD, stored)
