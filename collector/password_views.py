"""Allow a logged-in staff user to change only their own password."""

from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .models import AuditLog


@sensitive_post_parameters("old_password", "new_password1", "new_password2")
@never_cache
@login_required(login_url="/admin/login/")
@require_http_methods(["GET", "POST"])
def admin_password_change(request):
    """Use Django's password validators and retain only the current session."""
    if not (request.user.is_active and request.user.is_staff):
        raise PermissionDenied

    form = PasswordChangeForm(user=request.user, data=request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        if request.user.check_password(form.cleaned_data["new_password1"]):
            form.add_error("new_password1", "新密码不能与当前密码相同。")
        else:
            with transaction.atomic():
                user = form.save()
                AuditLog.objects.create(
                    actor=user,
                    action="admin_password_change",
                    target_type="User",
                    target_id=str(user.pk),
                    detail={},
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
            update_session_auth_hash(request, user)
            messages.success(request, "登录密码已更新，请使用新密码登录其他设备。")
            return redirect("admin-password-change")

    return render(request, "collector/admin_password_change.html", {"form": form})
