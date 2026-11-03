from django.http import HttpResponse
from .models import AppSetting


class UploadGateMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path_info.rstrip("/") == "/submit" and not AppSetting.get_bool("public_upload_open", True):
            return HttpResponse("公开上传已关闭，请联系管理员。", status=403, content_type="text/plain; charset=utf-8")
        return self.get_response(request)

