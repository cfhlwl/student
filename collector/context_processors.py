from django.conf import settings
from .maintenance import public_upload_is_open


def site_context(request):
    return {
        "site_name": settings.SITE_NAME,
        "upload_open": public_upload_is_open(),
    }

