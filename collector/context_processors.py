from django.conf import settings
from .models import AppSetting


def site_context(request):
    return {
        "site_name": settings.SITE_NAME,
        "upload_open": AppSetting.get_bool("public_upload_open", True),
    }

