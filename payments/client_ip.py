"""Optional local reverse-proxy IP handling; never trust an arbitrary XFF chain."""
import ipaddress
from django.conf import settings


def gateway_client_ip(request):
    peer = ipaddress.ip_address(request.META.get('REMOTE_ADDR',''))
    if settings.COPAY_TRUST_LOOPBACK_PROXY and peer.is_loopback and request.META.get('HTTP_X_REAL_IP'):
        return str(ipaddress.ip_address(request.META['HTTP_X_REAL_IP']))
    return str(peer)
