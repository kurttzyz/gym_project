"""Protocol/config tests: no real COPAY requests, payouts or database writes."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, override_settings
from payments.gateway_adapters.copay import CopayConfig, CopayConfigurationError
from platform_core.collections import gateway


@override_settings(PLATFORM_HOSTS=['platform.example.com'],COPAY_CONFIG_FILE='')
class CentralConfigurationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private = rsa.generate_private_key(public_exponent=65537,key_size=2048)

    def config_file(self,root,**changes):
        root.joinpath('private.pem').write_bytes(self.private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        root.joinpath('public.pem').write_bytes(self.private.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo))
        data=dict(environment='sandbox',merchant_code='CENTRAL-TEST',aes_key='test-central-key',
            merchant_private_key_file='private.pem',copay_public_key_file='public.pem',
            public_origin='https://platform.example.com',payment_method='GCQR0',cashier_hosts=[])
        data.update(changes)
        path=root/'central.json';path.write_text(json.dumps(data))
        return path

    def test_exact_platform_origin_and_key_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path=self.config_file(Path(directory))
            with override_settings(CENTRAL_COPAY_CONFIG_FILE=str(path)):
                config=gateway.central_config()
            self.assertEqual(config.environment,'sandbox')
            self.assertEqual(config.merchant_code,'CENTRAL-TEST')

    def test_wrong_origin_methods_or_environment_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            for changes in [dict(public_origin='https://gym.example.com'),dict(public_origin='http://platform.example.com'),
                    dict(public_origin='https://platform.example.com/path'),dict(payment_method='INVALID'),
                    dict(environment='invalid'),dict(cashier_hosts=['*.example.com'])]:
                path=self.config_file(Path(directory),**changes)
                with self.subTest(changes=changes),override_settings(CENTRAL_COPAY_CONFIG_FILE=str(path)),self.assertRaises(CopayConfigurationError):
                    gateway.central_config()

    def test_direct_merchant_cannot_be_reused_as_central_merchant(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=self.config_file(root)
            direct=root/'direct.json';direct.write_text(json.dumps({'gym-id':{'merchant_code':'CENTRAL-TEST','environment':'sandbox'}}))
            with override_settings(CENTRAL_COPAY_CONFIG_FILE=str(path),COPAY_CONFIG_FILE=str(direct)),self.assertRaises(CopayConfigurationError):
                gateway.central_config()

    def test_gateway_has_central_notify_and_gym_return_and_server_amount(self):
        config=CopayConfig('CENTRAL','key',self.private,self.private.public_key(),'sandbox','GCQR0','https://platform.example.com',())
        gym=SimpleNamespace(pk=1)
        order=SimpleNamespace(pk=uuid4(),merchant_reference='GTEST',currency='PHP',amount='1250.50',payment_method='GCQR0')
        member=SimpleNamespace(pk=uuid4())
        client=gateway.CentralGateway(replace(config,public_origin='https://gym.example.com'),gym)
        with patch.object(gateway,'register_collection') as register,patch.object(gateway,'central_config',return_value=config),patch.object(client,'request',return_value={}) as request:
            client.submit(order,member,'203.0.113.1')
        payload=request.call_args.args[1]
        self.assertEqual(payload['NotifyUrl'],'https://platform.example.com/collections/copay/notify/')
        self.assertEqual(payload['ReturnUrl'],f'https://gym.example.com/payments/{order.pk}/return/')
        self.assertEqual(payload['Amount'],order.amount)
        register.assert_called_once_with(gym,order)


class GatewayClientIPTests(SimpleTestCase):
    @override_settings(COPAY_TRUST_LOOPBACK_PROXY=False)
    def test_forwarded_headers_are_ignored_by_default(self):
        from payments.client_ip import gateway_client_ip
        request=SimpleNamespace(META={'REMOTE_ADDR':'127.0.0.1','HTTP_X_REAL_IP':'203.0.113.1'})
        self.assertEqual(gateway_client_ip(request),'127.0.0.1')

    @override_settings(COPAY_TRUST_LOOPBACK_PROXY=True)
    def test_only_loopback_peers_can_supply_single_real_ip(self):
        from payments.client_ip import gateway_client_ip
        request=SimpleNamespace(META={'REMOTE_ADDR':'127.0.0.1','HTTP_X_REAL_IP':'203.0.113.1'})
        self.assertEqual(gateway_client_ip(request),'203.0.113.1')
        request.META['REMOTE_ADDR']='198.51.100.1'
        self.assertEqual(gateway_client_ip(request),'198.51.100.1')
        request.META['REMOTE_ADDR']='127.0.0.1'
        request.META['HTTP_X_REAL_IP']='203.0.113.1,198.51.100.1'
        with self.assertRaises(ValueError):gateway_client_ip(request)
