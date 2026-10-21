from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User

admin.site.register(User, UserAdmin)

# Security delivery diagnostics are visible only to platform superusers through
# PowerhouseAdminSite, never through a tenant-owner page. No reset token is stored.
from .models import PasswordRecovery


@admin.register(PasswordRecovery)
class PasswordRecoveryAdmin(admin.ModelAdmin):
    list_display = ('id','user','gym','status','attempts','created_at','expires_at','outcome')
    list_filter = ('status',)
    search_fields = ('user__username','recipient','gym__name')
    fields = ('id','user','gym','recipient','origin','created_at','expires_at','used_at',
              'status','attempts','available_at','lease_until','outcome')
    readonly_fields = fields

    def has_add_permission(self,request):
        return False

    def has_change_permission(self,request,obj=None):
        return False

    def has_delete_permission(self,request,obj=None):
        return False
