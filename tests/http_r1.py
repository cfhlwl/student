"""Docker + Nginx real-HTTP smoke test for R1.

Run inside the web container while the nginx service is up:
    R1_BASE_URL=http://nginx R1_ADMIN_PASSWORD=... python tests/http_r1.py
"""

import hashlib
import html
import io
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
import zipfile

from openpyxl import Workbook, load_workbook
from PIL import Image


BASE_URL = os.getenv("R1_BASE_URL", "http://nginx").rstrip("/")
ADMIN_USER = os.getenv("R1_ADMIN_USER", "r1-http-admin")
ADMIN_PASSWORD = os.environ["R1_ADMIN_PASSWORD"]


def csrf_token(text):
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text)
    if not match:
        raise AssertionError("CSRF token not found")
    return html.unescape(match.group(1))


def multipart(fields, files):
    boundary = "----CodexR1" + uuid.uuid4().hex
    chunks = []
    for name, value in fields:
        chunks += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), str(value).encode(), b"\r\n"]
    for name, filename, content, content_type in files:
        chunks += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(), f"Content-Type: {content_type}\r\n\r\n".encode(), content, b"\r\n"]
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class HttpClient:
    def __init__(self):
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def request(self, path, method="GET", data=None, headers=None):
        request = urllib.request.Request(BASE_URL + path, data=data, method=method, headers=headers or {})
        with self.opener.open(request, timeout=120) as response:
            return response.status, response.headers, response.read()


def workbook_bytes():
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    for index in range(50):
        sheet.append([f"HTTP学生{index:02d}", "男" if index % 2 else "女", "高中部", "2026", "1", "走读", "R1真实HTTP虚构数据"])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def photo_bytes(index):
    output = io.BytesIO()
    Image.new("RGB", (40, 40), (index % 255, 80, 60)).save(output, format="JPEG")
    return output.getvalue()


def main():
    client = HttpClient()
    status, headers, page = client.request("/submit")
    assert status == 200
    csrf = csrf_token(page.decode("utf-8"))
    common = {"X-CSRFToken": csrf, "Referer": BASE_URL + "/submit"}
    roster = workbook_bytes()
    photos = [(f"HTTP学生{i:02d}.jpg", photo_bytes(i)) for i in range(50)]
    manifest = [{"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in photos]
    roster_fingerprint = hashlib.sha256(roster).hexdigest()
    body, content_type = multipart(
        [("expected_photo_count", "50"), ("photo_manifest", json.dumps(manifest, ensure_ascii=False)), ("roster_fingerprint", roster_fingerprint), ("note", "R1真实HTTP")],
        [("roster", "名单.xlsx", roster, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
    )
    common["Content-Type"] = content_type
    status, _, raw = client.request("/submit/upload/start/", "POST", body, common)
    assert status == 200, raw
    state = json.loads(raw)
    for batch_no in range(3):
        files = [("photos", name, payload, "image/jpeg") for name, payload in photos[batch_no * 20:batch_no * 20 + (20 if batch_no < 2 else 10)]]
        body, content_type = multipart([("batch_no", str(batch_no)), ("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_fingerprint)], files)
        common["Content-Type"] = content_type
        status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/photos/", "POST", body, common)
        assert status == 200, raw
    body, content_type = multipart([("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_fingerprint)], [])
    common["Content-Type"] = content_type
    status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/complete/", "POST", body, common)
    assert status == 200, raw
    result = json.loads(raw)
    assert result["submission_id"]

    status, _, login_page = client.request("/admin/login/")
    assert status == 200
    login_token = csrf_token(login_page.decode("utf-8"))
    login_body = urllib.parse.urlencode({"username": ADMIN_USER, "password": ADMIN_PASSWORD, "csrfmiddlewaretoken": login_token}).encode()
    status, _, _ = client.request("/admin/login/", "POST", login_body, {"Content-Type": "application/x-www-form-urlencoded", "Referer": BASE_URL + "/admin/login/", "X-CSRFToken": login_token})
    assert status in {200, 302}
    status, _, submissions_page = client.request("/admin/submissions/")
    assert status == 200
    review_token = csrf_token(submissions_page.decode("utf-8"))
    review_body = urllib.parse.urlencode({"action": "approved", "csrfmiddlewaretoken": review_token}).encode()
    status, _, _ = client.request(f"/admin/submissions/{result['submission_id']}/review/", "POST", review_body, {"Content-Type": "application/x-www-form-urlencoded", "Referer": BASE_URL + "/admin/submissions/", "X-CSRFToken": review_token})
    assert status in {200, 302}
    status, _, payload = client.request("/admin/export.zip?school_year=2026&class_name=1")
    assert status == 200 and payload[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        photo_names = [name for name in archive.namelist() if name.lower().endswith(".jpg")]
        assert len(photo_names) == 50
        stats = [name for name in archive.namelist() if name.endswith("学生统计表.xlsx")]
        assert stats
        book = load_workbook(io.BytesIO(archive.read(stats[0])), data_only=True)
        assert isinstance(book["统计汇总"]["B2"].value, int)
    print("HTTP_R1_OK photos=50 zip_bytes=" + str(len(payload)))


if __name__ == "__main__":
    main()
