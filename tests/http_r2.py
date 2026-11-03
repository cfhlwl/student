"""Docker + Nginx real-HTTP R2 test with 20 photos of at least 2.5 MB each.

Run inside the isolated web container while nginx is up:
    R2_BASE_URL=http://nginx R2_ADMIN_PASSWORD=... python tests/http_r2.py
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


BASE_URL = os.getenv("R2_BASE_URL", "http://nginx").rstrip("/")
ADMIN_USER = os.getenv("R2_ADMIN_USER", "r2-http-admin")
ADMIN_PASSWORD = os.environ["R2_ADMIN_PASSWORD"]
MIN_PHOTO_BYTES = 2_500_000
MAX_BATCH_BYTES = 32 * 1024 * 1024


def csrf_token(text):
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text)
    if not match:
        raise AssertionError("CSRF token not found")
    return html.unescape(match.group(1))


def multipart(fields, files):
    boundary = "----CodexR2" + uuid.uuid4().hex
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
        with self.opener.open(request, timeout=180) as response:
            return response.status, response.headers, response.read()


def roster_bytes():
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "班级", "就读类型", "备注"])
    for index in range(20):
        sheet.append([f"R2大文件{index:02d}", "男" if index % 2 else "女", "高中部", "2026", "2", "走读" if index % 3 else "住宿", "R2虚构数据"])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def photo_bytes(index):
    for edge in (1800, 1900, 2000, 2200):
        output = io.BytesIO()
        Image.effect_noise((edge, edge), 100).convert("RGB").save(output, format="JPEG", quality=95, optimize=False)
        payload = output.getvalue()
        if len(payload) >= MIN_PHOTO_BYTES:
            return payload
    raise AssertionError("could not generate a 2.5 MB valid JPEG")


def main():
    client = HttpClient()
    status, _, page = client.request("/submit")
    assert status == 200
    csrf = csrf_token(page.decode("utf-8"))
    common = {"X-CSRFToken": csrf, "Referer": BASE_URL + "/submit"}
    photos = [(f"R2大文件{index:02d}.jpg", photo_bytes(index)) for index in range(20)]
    photo_manifest = [{"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in photos]
    roster = roster_bytes()
    body, content_type = multipart(
        [("expected_photo_count", "20"), ("photo_manifest", json.dumps(photo_manifest, ensure_ascii=False)), ("roster_fingerprint", hashlib.sha256(roster).hexdigest()), ("note", "R2真实大小HTTP")],
        [("roster", "名单.xlsx", roster, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
    )
    common["Content-Type"] = content_type
    status, _, raw = client.request("/submit/upload/start/", "POST", body, common)
    assert status == 200, raw
    state = json.loads(raw)

    batches = []
    current = []
    current_size = 0
    for item in photos:
        if current and current_size + len(item[1]) > MAX_BATCH_BYTES:
            batches.append(current)
            current = []
            current_size = 0
        current.append(item)
        current_size += len(item[1])
    if current:
        batches.append(current)
    assert len(batches) >= 2
    for batch_no, batch in enumerate(batches):
        files = [("photos", name, payload, "image/jpeg") for name, payload in batch]
        body, content_type = multipart(
            [("batch_no", str(batch_no)), ("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", hashlib.sha256(roster).hexdigest())],
            files,
        )
        assert len(body) < 40 * 1024 * 1024
        common["Content-Type"] = content_type
        status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/photos/", "POST", body, common)
        assert status == 200, raw

    body, content_type = multipart(
        [("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", hashlib.sha256(roster).hexdigest())],
        [],
    )
    common["Content-Type"] = content_type
    status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/complete/", "POST", body, common)
    assert status == 200, raw
    result = json.loads(raw)

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

    status, _, payload = client.request("/admin/export.zip?school_year=2026&class_name=2")
    assert status == 200 and payload[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        photo_names = [name for name in archive.namelist() if name.lower().endswith(".jpg")]
        assert len(photo_names) == 20
        expected = {name: hashlib.sha256(payload).hexdigest() for name, payload in photos}
        for name in photo_names:
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected[name.rsplit("/", 1)[-1]]
        stats = [name for name in archive.namelist() if name.endswith("学生统计表.xlsx")]
        assert stats
        books = [load_workbook(io.BytesIO(archive.read(name)), data_only=True) for name in stats]
        assert sum(book["统计汇总"]["B2"].value for book in books) == 20
        assert all(isinstance(book["统计汇总"]["B8"].value, int) for book in books)
    print(f"HTTP_R2_OK photos=20 batches={len(batches)} max_photo_bytes={min(len(payload) for _, payload in photos)} zip_bytes={len(payload)}")


if __name__ == "__main__":
    main()
