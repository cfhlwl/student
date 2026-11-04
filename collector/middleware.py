import time

from django.db.utils import OperationalError
from django.http import HttpResponse, JsonResponse
from .models import AppSetting
from .maintenance import read_state
from .security_controls import classify_rate_limit, client_ip_from_meta, consume_rate_limit


class TrustedProxyMiddleware:
    """Interpret forwarding headers only from configured reverse proxies."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        peer = request.META.get("REMOTE_ADDR", "")
        resolved = client_ip_from_meta(request.META)
        if resolved != peer:
            request.META["REMOTE_ADDR"] = resolved
        else:
            request.META.pop("HTTP_X_FORWARDED_FOR", None)
            request.META.pop("HTTP_X_REAL_IP", None)
            request.META.pop("HTTP_X_FORWARDED_PROTO", None)
        return self.get_response(request)


class CollectionRateLimitMiddleware:
    """Rate-limit public collection endpoints using a shared file cache."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        name = classify_rate_limit(request.path_info, request.method)
        if name:
            allowed, retry_after = consume_rate_limit(name=name, client_ip=client_ip_from_meta(request.META))
            if not allowed:
                response = JsonResponse({"error": "请求过于频繁，请稍后重试。", "retry_after": retry_after}, status=429)
                response["Retry-After"] = str(retry_after)
                response["Cache-Control"] = "no-store"
                return response
        return self.get_response(request)


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
            if read_state() != "open":
                return HttpResponse("采集入口正在维护或已关闭，请联系管理员。", status=403, content_type="text/plain; charset=utf-8")
        if upload_path and not gate_open:
            return HttpResponse("公开上传已关闭，请联系管理员。", status=403, content_type="text/plain; charset=utf-8")
        return self.get_response(request)

