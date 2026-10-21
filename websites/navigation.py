"""Role-aware navigation; these links never replace view authorization."""
from django.urls import reverse


def workspace_navigation(request,role,modules):
    name=getattr(getattr(request,'resolver_match',None),'url_name','')
    groups=[]
    def group(label,items):
        links=[]
        for route,title,icon,related in items:
            links.append({'url':reverse(route),'title':title,'icon':icon,'active':name==route or name in related})
        if links:groups.append({'label':label,'links':links,'expanded':any(link['active'] for link in links) or label=='Workspace' or role in {'member','staff','manager'} or request.tenant is None or (name=='owner-dashboard' and label in {'Membership','Gym operations'})})
    def item(route,title,icon,*related):return (route,title,icon,related)
    if request.tenant is None:
        if not request.user.is_authenticated or not request.user.is_superuser:return [],'',''
        group('Workspace',[item('dashboard','Overview','grid'),item('gym-add','Add a gym','plus'),
            item('saas-dashboard','Subscriptions','wallet','saas-invoice','saas-subscription'),item('saas-plans','Software plans','layers','saas-plan-new','saas-plan-edit')])
        group('Platform',[item('admin:index','Administration','shield')])
        return groups,'Platform workspace',reverse('dashboard')
    if not role:return [],'Member access',reverse('home')
    if role=='owner':
        group('Workspace',[item('owner-dashboard','Overview','grid')])
        if modules.get('memberships'):
            group('Membership',[item('owner-members','Members','users','member-new','member-detail','member-edit'),
                item('owner-plans','Membership plans','card','owner-plan-new','owner-plan-edit'),
                item('owner-applications','Applications','clipboard'),item('owner-renewals','Renewal requests','refresh')])
        operations=[]
        if modules.get('attendance'):operations.append(item('attendance-scanner','Front desk','scan'))
        if modules.get('attendance'):operations.append(item('attendance-history','Attendance','activity'))
        if modules.get('memberships'):operations.extend([item('class-schedule','Class schedule','calendar','class-roster','class-new'),item('class-trainers','Trainers','dumbbell','class-trainer-edit')])
        group('Gym operations',operations)
        if modules.get('pos'):group('Retail',[item('pos-checkout','Point of sale','bag','pos-confirm'),item('pos-products','Products','box','pos-product-new','pos-product-edit'),item('pos-inventory','Inventory','layers','pos-adjustment'),item('pos-sales','Sales & receipts','receipt','pos-receipt','pos-refund'),item('pos-report','Daily sales','chart')])
        finance=[]
        if modules.get('payments'):finance.append(item('owner-payments','Member payments','wallet','payment-detail','payment-receipt'))
        group('Finance',finance)
        business=[]
        if modules.get('websites'):business.append(item('website-edit','Website & branding','globe'))
        if modules.get('websites') and modules.get('memberships'):business.append(item('owner-inbox','Gym inbox','mail'))
        if any(modules.get(key) for key in ['memberships','attendance','pos']):business.extend([item('attendance-locations','Branches','pin','attendance-location-edit'),item('attendance-staff','Staff access','shield')])
        business.extend([item('owner-notifications','Notifications','bell','notification-detail'),item('tenant-saas-billing','Software subscription','settings','tenant-saas-invoice')])
        group('Your business',business)
        return groups,'Gym management',reverse('owner-dashboard')
    if role in {'manager','staff'}:
        operations=[]
        if modules.get('attendance'):operations.extend([item('attendance-scanner','Front desk','scan'),item('attendance-history','Attendance','activity')])
        if modules.get('memberships'):operations.append(item('class-schedule','Class schedule','calendar','class-roster'))
        if modules.get('pos'):operations.extend([item('pos-checkout','Point of sale','bag'),item('pos-sales','My sales & receipts','receipt','pos-receipt')])
        group('Gym operations',operations)
        return groups,'Staff workspace',groups[0]['links'][0]['url'] if groups else reverse('dashboard')
    links=[item('dashboard','Overview','grid')]
    if modules.get('memberships'):links.extend([item('member-classes','Classes & bookings','calendar'),item('member-renewals','Renew membership','refresh')])
    if modules.get('attendance'):links.extend([item('member-card','My member card','scan'),item('member-attendance','My attendance','activity')])
    links.append(item('member-payments','Payment history','wallet','payment-detail','payment-receipt'))
    group('My gym',links)
    group('My account',[item('member-profile','My profile','users'),item('member-email-preferences','Email preferences','bell'),item('member-password-change','Password & security','shield')])
    return groups,'Member portal',reverse('dashboard')
