import time

from django.db.utils import OperationalError
from django.http import HttpResponse
from .models import AppSetting


class UploadGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        upload_path = request.path_info.rstrip("/") == "/submit" or request.path_info.startswith("/submit/upload/")
        gate_open = True
        if upload_path:
            for attempt in range(20):
                try:
                    gate_open = AppSetting.get_bool("public_upload_open", True)
                    break
                except OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 19:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        if upload_path and not gate_open:
            return HttpResponse("公开上传已关闭，请联系管理员。", status=403, content_type="text/plain; charset=utf-8")
        return self.get_response(request)

