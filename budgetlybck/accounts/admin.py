from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import PolicyAcceptance, PolicyDocument, User


admin.site.register(User, UserAdmin)


@admin.register(PolicyDocument)
class PolicyDocumentAdmin(admin.ModelAdmin):
    list_display = ('document_type', 'version', 'is_active', 'published_at', 'created_at')
    list_filter = ('document_type', 'is_active')
    search_fields = ('version', 'content')
    readonly_fields = ('content_sha256', 'published_at', 'created_at')


@admin.register(PolicyAcceptance)
class PolicyAcceptanceAdmin(admin.ModelAdmin):
    list_display = ('user', 'document', 'accepted_at')
    list_select_related = ('user', 'document')
    search_fields = ('user__username', 'user__email', 'document__version')
    readonly_fields = ('user', 'document', 'accepted_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
