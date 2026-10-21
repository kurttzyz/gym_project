import re
from django.db import DatabaseError
from gyms.databases import GymDatabaseUnavailable
from gyms.models import Membership
from platform_core.billing.services import access_for_gym
from .services import website_for_request
from .navigation import workspace_navigation


def branding(request):
    gym=getattr(request,'tenant',None)
    site=None
    if gym and not getattr(request,'platform_billing_only',False):
        try:site=website_for_request(request)
        except (GymDatabaseUnavailable,DatabaseError):pass
    access_record=Membership.objects.filter(organization=gym,user=request.user,is_active=True).first() if gym and request.user.is_authenticated else None
    role=access_record.role if access_record else ''
    access=getattr(request,'saas_access',None)
    if gym and role and access is None:
        try:access=access_for_gym(gym)
        except DatabaseError:pass
    modules=access.modules if access and access.allowed else {}
    groups,kind,home=workspace_navigation(request,role,modules)
    color=site.primary_color if site else (gym.primary_color if gym else '#c5ed91')
    if not re.fullmatch(r'#[0-9a-fA-F]{6}',color):color='#245ac5'
    rgb=[int(color[i:i+2],16)/255 for i in (1,3,5)]
    linear=[c/12.92 if c<=.04045 else ((c+.055)/1.055)**2.4 for c in rgb]
    light=sum(c*w for c,w in zip(linear,[.2126,.7152,.0722]))>.179
    active=next((link['title'] for group in groups for link in group['links'] if link['active']),'Workspace')
    return {'tenant':gym,'branding_name':site.display_name if site else (gym.name if gym else 'Powerhouse'),
        'branding_color':color,'branding_ink':'#000000' if light else '#ffffff',
        'branding_logo':site.logo_url if site else (gym.logo_url if gym else ''),
        'is_gym_owner':role=='owner','is_front_desk':role in {'owner','manager','staff'},
        'is_pos_staff':bool(role in {'owner','manager','staff'} and modules.get('pos')),
        'workspace_groups':groups,'workspace_kind':kind,'workspace_home':home,'workspace_page':active,
        'workspace_role':role,'workspace_modules':modules,'has_workspace':bool(groups)}
