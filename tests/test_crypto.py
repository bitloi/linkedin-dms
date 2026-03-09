"""Tests for libs.core.crypto — encryption at rest."""

from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

from libs.core import crypto
from libs.core.crypto import decrypt_if_encrypted, encrypt_if_configured, validate_fernet_key


@pytest.fixture(autouse=True)
def _reset_warning_flag():
    """Reset the module-level warning flag between tests."""
    crypto._warned_no_key = False
    yield
    crypto._warned_no_key = False


@pytest.fixture()
def fernet_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", key)
    return key


@pytest.fixture()
def no_key(monkeypatch):
    monkeypatch.delenv("DESEARCH_ENCRYPTION_KEY", raising=False)


class TestEncryptIfConfigured:
    def test_encrypts_with_key(self, fernet_key):
        plaintext = '{"li_at": "secret_cookie", "jsessionid": null}'
        ciphertext = encrypt_if_configured(plaintext)
        assert ciphertext != plaintext
        f = Fernet(fernet_key.encode())
        assert f.decrypt(ciphertext.encode()).decode() == plaintext

    def test_passthrough_without_key(self, no_key):
        plaintext = '{"li_at": "secret_cookie"}'
        assert encrypt_if_configured(plaintext) == plaintext

    def test_different_calls_produce_different_ciphertext(self, fernet_key):
        plaintext = "same_input"
        c1 = encrypt_if_configured(plaintext)
        c2 = encrypt_if_configured(plaintext)
        assert c1 != c2  # Fernet includes a timestamp nonce


class TestDecryptIfEncrypted:
    def test_decrypts_with_key(self, fernet_key):
        plaintext = '{"li_at": "secret_cookie"}'
        ciphertext = encrypt_if_configured(plaintext)
        assert decrypt_if_encrypted(ciphertext) == plaintext

    def test_passthrough_without_key(self, no_key):
        plaintext = '{"li_at": "secret_cookie"}'
        assert decrypt_if_encrypted(plaintext) == plaintext

    def test_legacy_plaintext_fallback(self, fernet_key):
        """Pre-encryption plaintext rows should be returned as-is (InvalidToken caught)."""
        legacy = '{"li_at": "old_cookie"}'
        result = decrypt_if_encrypted(legacy)
        assert result == legacy

    def test_empty_string(self, fernet_key):
        assert decrypt_if_encrypted("") == ""


class TestRoundTrip:
    def test_encrypt_then_decrypt(self, fernet_key):
        original = '{"li_at": "AQEDAWx0Y29va2ll", "jsessionid": "ajax:tok"}'
        encrypted = encrypt_if_configured(original)
        decrypted = decrypt_if_encrypted(encrypted)
        assert decrypted == original

    def test_roundtrip_unicode(self, fernet_key):
        original = '{"text": "héllo wörld 🔐"}'
        assert decrypt_if_encrypted(encrypt_if_configured(original)) == original


class TestValidateFernetKey:
    def test_valid_key(self):
        key = Fernet.generate_key().decode()
        result = validate_fernet_key(key)
        assert isinstance(result, bytes)
        assert len(result) == 44

    def test_too_short_raises(self):
        with pytest.raises(ValueError, match="44 characters"):
            validate_fernet_key("tooshort")

    def test_too_long_raises(self):
        with pytest.raises(ValueError, match="44 characters"):
            validate_fernet_key("a" * 100)

    def test_invalid_base64_raises(self):
        with pytest.raises(ValueError):
            validate_fernet_key("!" * 44)


class TestBadKeyEnv:
    def test_bad_key_raises_on_encrypt(self, monkeypatch):
        monkeypatch.setenv("DESEARCH_ENCRYPTION_KEY", "x" * 44)
        with pytest.raises(ValueError, match="not a valid Fernet key"):
            encrypt_if_configured("test")
