import uuid
from django.db import models


class RenewalRequest(models.Model):
    id = models.UUIDField(primary_key=True,default=uuid.uuid4,editable=False)
    member = models.ForeignKey('gym_core.MemberProfile',on_delete=models.PROTECT,related_name='renewal_requests')
    plan = models.ForeignKey('gym_core.MembershipPlan',on_delete=models.PROTECT)
    plan_name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10,decimal_places=2)
    period = models.CharField(max_length=10)
    starts_on = models.DateField()
    expires_on = models.DateField()
    note = models.TextField(max_length=1000,blank=True)
    status = models.CharField(max_length=12,default='pending',choices=[('pending','Awaiting review'),('approved','Approved'),('declined','Declined'),('cancelled','Cancelled')])
    subscription = models.OneToOneField('gym_core.MemberSubscription',null=True,blank=True,on_delete=models.PROTECT)
    requested_by_id = models.PositiveBigIntegerField()
    reviewed_by_id = models.PositiveBigIntegerField(null=True,blank=True)
    review_note = models.CharField(max_length=500,blank=True)
    reviewed_at = models.DateTimeField(null=True,blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at','id']
        constraints = [models.UniqueConstraint(fields=['member'],condition=models.Q(status='pending'),name='one_pending_member_renewal'),
            models.CheckConstraint(condition=models.Q(price__gte=0),name='renewal_nonnegative_price'),
            models.CheckConstraint(condition=models.Q(expires_on__gt=models.F('starts_on')),name='renewal_positive_term')]


class MemberProfileChange(models.Model):
    member = models.ForeignKey('gym_core.MemberProfile',on_delete=models.PROTECT,related_name='profile_changes')
    actor_id = models.PositiveBigIntegerField()
    changes = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at','-id']

    @property
    def changed_fields(self):
        from gym_core.members.models import MemberProfile
        return [str(MemberProfile._meta.get_field(name).verbose_name).title() for name in self.changes]
