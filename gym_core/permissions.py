from functools import wraps
from django.contrib.auth.decorators import login_required
from django.http import Http404
from gyms.models import Membership


def owner_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if request.tenant is None or not Membership.objects.filter(
            organization=request.tenant, user=request.user,
            role=Membership.Role.OWNER, is_active=True,
        ).exists():
            raise Http404
        return view(request, *args, **kwargs)
    return wrapped
