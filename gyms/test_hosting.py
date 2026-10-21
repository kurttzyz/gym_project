"""No real DNS, ACME requests, Nginx reloads, or production databases."""
from datetime import timedelta
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from gyms import hosting

spec = importlib.util.spec_from_file_location('hosting_agent', Path(__file__).resolve().parent.parent / 'deploy/hosting/agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class HostingAgentTests(SimpleTestCase):
    def test_rejects_hostname_injection_and_local_hosts(self):
        for value in ['x.com;id', '../example.com', 'https://example.com', 'a.localhost', 'a.test', '127.0.0.1', '*.example.com', 'a.com\n', None]:
            with self.subTest(value=value), self.assertRaises(agent.SetupError):
                agent.hostname(value)
        self.assertEqual(agent.hostname('gym.example.com'), 'gym.example.com')

    def test_dns_requires_proof_and_exact_origin(self):
        resolver = Mock()
        txt = SimpleNamespace(strings=[b'a' * 43])
        resolver.resolve.side_effect = [[txt], ['203.0.113.1', '203.0.113.2']]
        with patch.object(agent.dns.resolver, 'Resolver', return_value=resolver):
            self.assertIn('A record', agent.check_dns('gym.example.com', 'a' * 43, {'ipv4': '203.0.113.1'}))
        resolver.resolve.side_effect = [[txt]]
        with patch.object(agent.dns.resolver, 'Resolver', return_value=resolver):
            self.assertIn('TXT', agent.check_dns('gym.example.com', 'b' * 43, {'ipv4': '203.0.113.1'}))

    def test_valid_dns_and_ipv6_mismatch(self):
        resolver = Mock()
        txt = SimpleNamespace(strings=[b'a' * 43])
        resolver.resolve.side_effect = [[txt], ['203.0.113.1'], agent.dns.resolver.NoAnswer()]
        with patch.object(agent.dns.resolver, 'Resolver', return_value=resolver):
            self.assertIsNone(agent.check_dns('gym.example.com', 'a' * 43, {'ipv4': '203.0.113.1'}))
        resolver.resolve.side_effect = [[txt], ['203.0.113.1'], ['::1']]
        with patch.object(agent.dns.resolver, 'Resolver', return_value=resolver):
            self.assertIn('AAAA', agent.check_dns('gym.example.com', 'a' * 43, {'ipv4': '203.0.113.1'}))

    def test_waiting_never_runs_privileged_commands(self):
        with patch.object(agent, 'check_dns', return_value='Waiting for DNS'), patch.object(agent, 'run') as run:
            result = agent.provision({'hostname': 'gym.example.com', 'token': 'a' * 43}, {'reserved_hosts': []})
        self.assertEqual(result['status'], 'waiting')
        run.assert_not_called()

    def test_reserved_domain_is_rejected(self):
        with self.assertRaises(agent.SetupError):
            agent.provision({'hostname': 'platform.example.com'}, {'reserved_hosts': ['platform.example.com']})

    def test_refuses_existing_sites_in_any_include(self):
        for name in ['gym.example.com', '*.example.com', '.example.com', '~^gym', '\n gym.example.com\n', 'gym.*']:
            with self.subTest(name=name), patch.object(agent, 'run', return_value=f'# configuration file /etc/nginx/sites-enabled/existing:\nserver_name {name};'), self.assertRaises(agent.SetupError):
                agent.refuse_existing_site('gym.example.com', Path('/different.conf'))

    def test_failed_nginx_validation_restores_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'gym.conf'
            target.write_text('previous')
            with patch.object(agent, 'MANAGED', Path(directory)), patch.object(agent, 'run', side_effect=[agent.SetupError('invalid'), '', '']):
                with self.assertRaises(agent.SetupError):
                    agent.write_config(target, 'invalid replacement')
            self.assertEqual(target.read_text(), 'previous')
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def test_symlink_cannot_replace_an_unmanaged_file(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / 'existing'
            original.write_text('keep')
            target = Path(directory) / 'managed.conf'
            target.symlink_to(original)
            with self.assertRaises(agent.SetupError):
                agent.write_config(target, 'replacement')
            self.assertEqual(original.read_text(), 'keep')

    def test_certificate_failure_cannot_mark_live(self):
        config = {'reserved_hosts': [], 'certbot': '/usr/bin/certbot', 'email': 'ops@example.com', 'certbot_config_dir':'/var/lib/powerhouse-certbot/config', 'certbot_work_dir':'/var/lib/powerhouse-certbot/work', 'certbot_logs_dir':'/var/lib/powerhouse-certbot/logs'}
        with patch.object(agent, 'check_dns', return_value=None), patch.object(agent, 'refuse_existing_site'), patch.object(agent, 'write_config') as write, patch.object(agent, 'run', side_effect=agent.SetupError('ACME failed')):
            with self.assertRaises(agent.SetupError):
                agent.provision({'hostname': 'gym.example.com', 'token': 'a' * 43}, config)
        self.assertEqual(write.call_count, 1)
        self.assertNotIn('listen 443', write.call_args.args[1])

    def test_http_challenge_does_not_expose_tenant_before_tls(self):
        self.assertNotIn('proxy_pass', agent.config_text('gym.example.com'))
        secure = agent.config_text('gym.example.com', tls=True)
        self.assertIn('proxy_set_header Host $host', secure)
        self.assertIn('ssl_certificate /var/lib/powerhouse-certbot/config/live/gym.example.com/', secure)

    def test_acme_and_reload_success(self):
        config = {'reserved_hosts': [], 'certbot': '/usr/bin/certbot', 'email': 'ops@example.com', 'certbot_config_dir':'/var/lib/powerhouse-certbot/config', 'certbot_work_dir':'/var/lib/powerhouse-certbot/work', 'certbot_logs_dir':'/var/lib/powerhouse-certbot/logs'}
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, 'MANAGED', Path(directory)), patch.object(agent, 'check_dns', return_value=None), patch.object(agent, 'refuse_existing_site'), patch.object(agent, 'write_config') as write, patch.object(agent, 'run') as run:
            result = agent.provision({'hostname': 'gym.example.com', 'token': 'a' * 43}, config)
        self.assertEqual(result['status'], 'live')
        self.assertEqual(write.call_count, 2)
        self.assertIn('--webroot', run.call_args.args[0])
        self.assertIn('--non-interactive', run.call_args.args[0])


@override_settings(DOMAIN_HOSTING_ENABLED=True)
class HostingWorkerTests(SimpleTestCase):
    def domain(self, **changes):
        values = dict(pk=1, is_active=True, hostname='gym.example.com', hosting_status='pending', hosting_next_attempt=None, save=Mock())
        values.update(changes)
        return SimpleNamespace(**values)

    def test_disabled_live_local_and_leased_domains_are_skipped(self):
        for changes in [dict(is_active=False), dict(hostname='gym.localhost'), dict(hosting_status='live'), dict(hosting_status='failed'), dict(hosting_next_attempt=timezone.now() + timedelta(minutes=5))]:
            with self.subTest(changes=changes), patch.object(hosting, 'transaction') as txn, patch.object(hosting, 'GymDomain') as model, patch.object(hosting, 'call_agent') as call:
                model.objects.select_for_update.return_value.get.return_value = self.domain(**changes)
                hosting.process_domain(1)
                call.assert_not_called()

    def test_agent_success_records_proof_and_live_status_after_lease(self):
        domain = self.domain()
        with patch.object(hosting, 'transaction'), patch.object(hosting, 'GymDomain') as model, patch.object(hosting, 'call_agent', return_value={'status': 'live', 'message': 'Ready'}):
            model.objects.select_for_update.return_value.get.return_value = domain
            hosting.process_domain(1)
        changes = model.objects.filter.return_value.update.call_args.kwargs
        self.assertEqual(changes['hosting_status'], 'live')
        self.assertIn('verified_at', changes)
        self.assertIsNone(changes['hosting_next_attempt'])
        domain.save.assert_called_once()

    def test_agent_outage_fails_safely_and_does_not_activate(self):
        with patch.object(hosting, 'transaction'), patch.object(hosting, 'GymDomain') as model, patch.object(hosting, 'call_agent', side_effect=OSError):
            model.objects.select_for_update.return_value.get.return_value = self.domain()
            hosting.process_domain(1)
        changes = model.objects.filter.return_value.update.call_args.kwargs
        self.assertEqual(changes['hosting_status'], 'failed')
        self.assertNotIn('verified_at', changes)

    @override_settings(DOMAIN_HOSTING_ENABLED=False)
    def test_opt_in_required(self):
        with patch.object(hosting, 'GymDomain') as model:
            hosting.process_domain(1)
            hosting.sweep_domains()
        model.objects.assert_not_called()


class ManagedCertificateRenewalTests(SimpleTestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location('hosting_renew', Path(__file__).resolve().parent.parent / 'deploy/hosting/renew.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_renewal_uses_separate_profile_before_nginx_reload(self):
        import json
        module = self.module()
        config = dict(certbot='/usr/local/bin/certbot',certbot_config_dir='/var/lib/powerhouse-certbot/config',
            certbot_work_dir='/var/lib/powerhouse-certbot/work',certbot_logs_dir='/var/lib/powerhouse-certbot/logs')
        with patch.object(module.Path,'read_text',return_value=json.dumps(config)),patch.object(module.subprocess,'run') as run:
            module.main()
        self.assertEqual(run.call_count,3)
        self.assertIn('/var/lib/powerhouse-certbot/config',run.call_args_list[0].args[0])
        self.assertEqual(run.call_args_list[1].args[0],['/usr/sbin/nginx','-t'])
        self.assertEqual(run.call_args_list[2].args[0],['/usr/bin/systemctl','reload','nginx'])

    def test_failed_renewal_does_not_reload_nginx(self):
        import json
        module = self.module()
        config = dict(certbot='/usr/local/bin/certbot',certbot_config_dir='/var/lib/powerhouse-certbot/config',
            certbot_work_dir='/var/lib/powerhouse-certbot/work',certbot_logs_dir='/var/lib/powerhouse-certbot/logs')
        with patch.object(module.Path,'read_text',return_value=json.dumps(config)),patch.object(module.subprocess,'run',side_effect=module.subprocess.CalledProcessError(1,'certbot')) as run:
            with self.assertRaises(module.subprocess.CalledProcessError):module.main()
        self.assertEqual(run.call_count,1)
