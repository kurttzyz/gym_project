"""COPAY+ V1.0.16 deposit protocol. No payout or recurring-charge API."""
import base64
import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
import simplejson
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from django.conf import settings


class CopayError(Exception):
    """Safe, generic error suitable for display; never include provider bodies/keys."""


class CopayConfigurationError(CopayError):
    pass


PHP_METHODS = {"GCP", "GCDR0", "GCDR1", "GCQR0", "GCQR1", "GCWU0", "GCWU1", "MYP"}
API_ORIGINS = {"sandbox": "https://mapi.marvelpay.net", "production": "https://mapi.copay.plus"}


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError
        return amount.quantize(Decimal("0.01"))
    except (ValueError, InvalidOperation, TypeError):
        raise CopayError("Invalid COPAY payment amount.") from None


def json_object(value):
    def pairs(items):
        result = {}
        for key, val in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = val
        return result
    try:
        result = json.loads(value, parse_float=Decimal, object_pairs_hook=pairs,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, TypeError, UnicodeError):
        raise CopayError("Invalid COPAY message.") from None


def load_key(value, *, private=False, allow_legacy_1024=False):
    minimum_bits = 1024 if allow_legacy_1024 is True else 2048
    raw = (value.encode() if isinstance(value, str) else value).strip()
    try:
        if raw.startswith(b"-----BEGIN"):
            key = (serialization.load_pem_private_key(raw, password=None) if private
                   else serialization.load_pem_public_key(raw))
        else:
            raw = base64.b64decode(b"".join(raw.split()), validate=True)
            key = (serialization.load_der_private_key(raw, password=None) if private
                   else serialization.load_der_public_key(raw))
        if not isinstance(key, rsa.RSAPrivateKey if private else rsa.RSAPublicKey) or key.key_size < minimum_bits:
            raise ValueError
        return key
    except (ValueError, TypeError):
        raise CopayConfigurationError(f"COPAY requires valid RSA keys of at least {minimum_bits} bits.") from None


@dataclass(frozen=True)
class CopayConfig:
    merchant_code: str
    aes_key: str
    private_key: object
    public_key: object
    environment: str
    payment_method: str
    public_origin: str
    cashier_hosts: tuple

    def validate_url(self, value):
        if not isinstance(value, str) or len(value) > 2000:
            raise CopayError("COPAY returned an invalid checkout URL.")
        try:
            url = urlsplit(value)
            domain = "marvelpay.net" if self.environment == "sandbox" else "copay.plus"
            approved = url.hostname and (url.hostname == domain or url.hostname.endswith("." + domain) or url.hostname in self.cashier_hosts)
            if (url.scheme != "https" or url.username or url.password or url.port not in (None, 443)
                    or not approved or any(c in value for c in "\r\n\\")):
                raise ValueError
        except ValueError:
            raise CopayError("COPAY returned an unapproved checkout URL.") from None
        return value


def config_for_gym(gym):
    """No global merchant fallback: each immutable gym UUID has its own secret entry."""
    if settings.CENTRAL_COLLECTIONS_ENABLED:
        from platform_core.collections.gateway import config_for_central_gym
        return config_for_central_gym(gym, new=True)
    try:
        path = Path(settings.COPAY_CONFIG_FILE)
        entries = json_object(path.read_text())
        data = entries[str(gym.database_key)]
        environment = data.get("environment", "sandbox")
        origin = data["public_origin"].rstrip("/")
        parsed = urlsplit(origin)
        if (environment not in API_ORIGINS or data["payment_method"] not in PHP_METHODS or
                parsed.scheme != "https" or parsed.path or parsed.query or parsed.fragment or
                parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError
        if not gym.domains.filter(hostname=parsed.hostname, verified_at__isnull=False, is_active=True).exists():
            raise ValueError
        if not data["merchant_code"] or len(data["merchant_code"]) > 100 or not data["aes_key"]:
            raise ValueError
        # A merchant account cannot be silently shared across tenants.
        for key, other in entries.items():
            if key != str(gym.database_key) and other.get("merchant_code") == data["merchant_code"] and other.get("environment", "sandbox") == environment:
                raise ValueError
        def read_key(name):
            key_path = Path(data[name])
            return (key_path if key_path.is_absolute() else path.parent / key_path).read_bytes()
        hosts = tuple(data.get("cashier_hosts", []))
        if any(not isinstance(host, str) or "/" in host or ":" in host or "*" in host for host in hosts):
            raise ValueError
        return CopayConfig(data["merchant_code"], data["aes_key"], load_key(read_key("merchant_private_key_file"), private=True),
                           load_key(read_key("copay_public_key_file")), environment, data["payment_method"], origin, hosts)
    except (OSError, KeyError, ValueError, TypeError, AttributeError, CopayError):
        raise CopayConfigurationError("COPAY is not configured for this gym. Contact the platform administrator.") from None


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CopayGateway:
    def __init__(self, config):
        self.config = config
        self.aes_key = hashlib.pbkdf2_hmac("sha1", config.aes_key.encode(), config.aes_key.encode(), 1000, dklen=32)

    def encrypt(self, plaintext):
        iv = os.urandom(16)
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(iv)).encryptor()
        return base64.b64encode(iv + encryptor.update(padded) + encryptor.finalize()).decode()

    def decrypt(self, encrypted):
        try:
            raw = base64.b64decode(encrypted, validate=True)
            if len(raw) < 32 or len(raw) % 16:
                raise ValueError
            decryptor = Cipher(algorithms.AES(self.aes_key), modes.CBC(raw[:16])).decryptor()
            padded = decryptor.update(raw[16:]) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        except (ValueError, TypeError):
            raise CopayError("Invalid COPAY message.") from None

    def envelope(self, data):
        plaintext = simplejson.dumps(data, use_decimal=True, ensure_ascii=False, separators=(",", ":")).encode()
        signature = self.config.private_key.sign(plaintext, rsa_padding.PKCS1v15(), hashes.SHA1())
        return {"MerchantCode": self.config.merchant_code, "Data": self.encrypt(plaintext),
                "Signature": base64.b64encode(signature).decode(), "Version": 0}

    def verified_callback(self, body):
        envelope = json_object(body)
        if type(envelope.get("Version")) is not int or envelope["Version"] != 0:
            raise CopayError("Unsupported COPAY version.")
        plaintext = self.decrypt(envelope.get("Data"))
        try:
            self.config.public_key.verify(base64.b64decode(envelope.get("Signature"), validate=True), plaintext,
                                          rsa_padding.PKCS1v15(), hashes.SHA1())
        except (InvalidSignature, ValueError, TypeError):
            raise CopayError("Invalid COPAY signature.") from None
        data = json_object(plaintext)
        if data.get("MerchantCode") != self.config.merchant_code or data.get("MerchantTransactionId") != envelope.get("MerchantTransactionId"):
            raise CopayError("COPAY merchant reference mismatch.")
        return data

    def request(self, path, data):
        envelope = self.envelope({"MerchantCode": self.config.merchant_code, **data})
        request = Request(API_ORIGINS[self.config.environment] + path, data=json.dumps(envelope).encode(),
                          headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        try:
            with build_opener(NoRedirects()).open(request, timeout=12) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise CopayError("COPAY response is too large.")
            result = json_object(raw)
            if result.get("isSuccess") is not True or not isinstance(result.get("data"), dict):
                raise CopayError("COPAY did not accept this request. Check the payment status before retrying.")
            return result["data"]
        except (HTTPError, URLError, TimeoutError, OSError):
            raise CopayError("COPAY could not be reached. Check status before creating another payment.") from None

    def submit(self, order, member, client_ip):
        return self.request("/api/Deposit/SubmitDeposit", {
            "MerchantTransactionId": order.merchant_reference, "MerchantClientId": member.id.hex,
            "CurrencyCode": order.currency, "Amount": order.amount, "PaymentMethodCode": order.payment_method,
            "NotifyUrl": self.config.public_origin + "/payments/copay/notify/",
            "ReturnUrl": self.config.public_origin + f"/payments/{order.pk}/return/", "IPAddress": client_ip,
        })

    def requery(self, order):
        return self.request("/api/Deposit/GetDepositDetail", {
            "MerchantTransactionId": order.merchant_reference, "CurrencyCode": order.currency,
        })
