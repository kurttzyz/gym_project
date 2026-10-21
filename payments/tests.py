"""Protocol checks with ephemeral test keys; never contact COPAY."""
import base64
import json
import unittest
from decimal import Decimal
from unittest.mock import patch, MagicMock
from urllib.error import URLError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa, padding as rsa_padding
from .gateway_adapters.copay import CopayConfig, CopayGateway, CopayError, json_object, money


class CopayProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.merchant = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.provider = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.config = CopayConfig("TEST", "Abcd1234", cls.merchant, cls.provider.public_key(),
                                "sandbox", "GCQR0", "https://example.test", ())

    def setUp(self):
        self.gateway = CopayGateway(self.config)

    def test_aes_matches_independent_openssl_vector_and_preserves_decimal(self):
        # Vector made using OpenSSL AES-256-CBC with the guide's PBKDF2 settings.
        expected = "AAECAwQFBgcICQoLDA0OD/agLcBjOmEs7sleJ4L7L0ck6kL4d7BQ6j123bjzPEUG2ijmD+nJHzTYlatR+1UNfg=="
        plaintext = b'{"Amount":100.00,"CurrencyCode":"PHP"}'
        with patch("payments.gateway_adapters.copay.os.urandom", return_value=bytes(range(16))):
            self.assertEqual(self.gateway.encrypt(plaintext), expected)
        self.assertEqual(self.gateway.decrypt(expected), plaintext)

    def test_outgoing_signature_covers_exact_plaintext_and_numeric_amount(self):
        envelope = self.gateway.envelope({"Amount":Decimal("1234.56"), "Name":"Member"})
        raw = self.gateway.decrypt(envelope["Data"])
        self.merchant.public_key().verify(base64.b64decode(envelope["Signature"]), raw,
                                         rsa_padding.PKCS1v15(), hashes.SHA1())
        self.assertIn(b'"Amount":1234.56', raw)
        self.assertEqual(envelope["MerchantCode"], "TEST")
        self.assertEqual(envelope["Version"], 0)

    def test_callback_verifies_original_bytes_without_json_reserialization(self):
        raw = b'{ "MerchantCode": "TEST", "MerchantTransactionId":"GTEST", "Amount":100.00 }'
        signature = self.provider.sign(raw, rsa_padding.PKCS1v15(), hashes.SHA1())
        envelope = {"Data":self.gateway.encrypt(raw), "Signature":base64.b64encode(signature).decode(),
                    "MerchantTransactionId":"GTEST", "Version":0}
        self.assertEqual(self.gateway.verified_callback(json.dumps(envelope))["Amount"], Decimal("100.00"))
        envelope["Data"] = self.gateway.encrypt(raw.replace(b'100.00',b'900.00'))
        with self.assertRaises(CopayError):
            self.gateway.verified_callback(json.dumps(envelope))

    def test_malformed_ciphertexts_duplicate_json_and_nonfinite_amounts_rejected(self):
        for value in ("garbage", "", None, base64.b64encode(b'0'*17).decode()):
            with self.subTest(value=value), self.assertRaises(CopayError):
                self.gateway.decrypt(value)
        for value in ('{"Status":6,"Status":7}', '[]', '{"Amount":NaN}'):
            with self.assertRaises(CopayError):
                json_object(value)
        for value in ("NaN", "Infinity", "1.001", "0", "-1", None):
            with self.assertRaises(CopayError):
                money(value)

    def test_checkout_urls_reject_injection_and_wrong_environment(self):
        self.assertEqual(self.config.validate_url("https://cashier.marvelpay.net/pay"), "https://cashier.marvelpay.net/pay")
        for value in ("https://copay.plus/pay", "https://marvelpay.net.attacker.test/pay", "javascript:alert(1)",
                      "https://cashier.marvelpay.net@evil.test/", "http://cashier.marvelpay.net/",
                      "https://cashier.marvelpay.net:bad/", "https://cashier.marvelpay.net/\r\nHeader:value"):
            with self.subTest(value=value), self.assertRaises(CopayError):
                self.config.validate_url(value)

    def test_transport_uses_pinned_api_endpoint_timeout_and_bounded_response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"isSuccess":true,"data":{"statusId":6}}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch("payments.gateway_adapters.copay.build_opener", return_value=opener):
            self.gateway.requery(type("Order", (), {"merchant_reference":"GTEST", "currency":"PHP"})())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://mapi.marvelpay.net/api/Deposit/GetDepositDetail")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12)
        response.read.assert_called_once_with(65537)
        envelope = json.loads(request.data)
        self.assertEqual(json_object(self.gateway.decrypt(envelope["Data"]))["MerchantTransactionId"], "GTEST")

    def test_transport_does_not_expose_response_details_or_retry(self):
        opener = MagicMock()
        opener.open.side_effect = URLError("sensitive provider failure")
        with patch("payments.gateway_adapters.copay.build_opener", return_value=opener), self.assertRaises(CopayError) as error:
            self.gateway.request("/api/Deposit/GetDepositDetail", {})
        self.assertNotIn("sensitive", str(error.exception))
        self.assertEqual(opener.open.call_count, 1)
