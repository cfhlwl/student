"""R2-FINAL real HTTP matrix against the isolated Nginx service.

Run inside the web container while the nginx service is up:
    R2F_BASE_URL=http://nginx R2F_ADMIN_PASSWORD=... python tests/http_r2_final.py
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from collector.models import Submission, SubmissionDocument, Student, StudentPhoto  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402
from PIL import Image  # noqa: E402


BASE_URL = os.getenv("R2F_BASE_URL", "http://nginx").rstrip("/")
ADMIN_USER = os.getenv("R2F_ADMIN_USER", "r2-final-http-admin")
ADMIN_PASSWORD = os.environ["R2F_ADMIN_PASSWORD"]
MAX_BATCH_BYTES = 32 * 1024 * 1024
MIXED_PHOTOS = 15


def csrf_token(text):
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', text)
    if not match:
        raise AssertionError("CSRF token not found")
    return html.unescape(match.group(1))


def multipart(fields, files):
    boundary = "----CodexR2Final" + uuid.uuid4().hex
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
        with self.opener.open(request, timeout=240) as response:
            return response.status, response.headers, response.read()


def roster_bytes(class_name, grade, count, prefix):
    department = "初中部" if grade.startswith("初") else "高中部"
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "备注"])
    for index in range(count):
        study_type = ["走读", "半走读", "住宿"][index % 3]
        sheet.append([f"{prefix}{index:02d}", "男" if index % 2 else "女", department, "2026", grade, class_name, study_type, "R2-FINAL虚构数据"])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def small_photo(index):
    output = io.BytesIO()
    Image.new("RGB", (180, 180), (index * 17 % 255, 80, 60)).save(output, format="JPEG", quality=90)
    return output.getvalue()


def large_photo(index):
    output = io.BytesIO()
    Image.effect_noise((1800, 1800), 100).convert("RGB").save(output, format="JPEG", quality=95, optimize=False)
    payload = output.getvalue()
    if len(payload) < 2_500_000:
        raise AssertionError("large photo did not reach 2.5 MB")
    return payload


def document_bytes():
    return b"%PDF-1.4\n" + b"R2-FINAL document\n" + b"0" * (20 * 1024 * 1024)


def split_batches(items):
    result, current, current_size = [], [], 0
    for item in items:
        if current and current_size + len(item[1]) > MAX_BATCH_BYTES:
            result.append(current)
            current, current_size = [], 0
        current.append(item)
        current_size += len(item[1])
    if current:
        result.append(current)
    return result


def submit(client, class_name, grade, count, prefix, with_document=False, large=False):
    status, _, page = client.request("/submit")
    assert status == 200
    csrf = csrf_token(page.decode("utf-8"))
    common = {"X-CSRFToken": csrf, "Referer": BASE_URL + "/submit"}
    photos = [(f"{prefix}{index:02d}.jpg", large_photo(index) if large else small_photo(index)) for index in range(count)]
    manifest = [{"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in photos]
    roster = roster_bytes(class_name, grade, count, prefix)
    roster_hash = hashlib.sha256(roster).hexdigest()
    expected_docs = 1 if with_document else 0
    body, content_type = multipart(
        [("expected_photo_count", str(count)), ("expected_document_count", str(expected_docs)), ("photo_manifest", json.dumps(manifest, ensure_ascii=False)), ("roster_fingerprint", roster_hash), ("note", "R2-FINAL真实HTTP")],
        [("roster", "名单.xlsx", roster, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
    )
    common["Content-Type"] = content_type
    status, _, raw = client.request("/submit/upload/start/", "POST", body, common)
    assert status == 200, raw
    state = json.loads(raw)
    batches = split_batches(photos)
    assert all(len(multipart([("batch_no", "0")], [("photos", n, p, "image/jpeg") for n, p in batch])[0]) < 40 * 1024 * 1024 for batch in batches)
    for batch_no, batch in enumerate(batches):
        body, content_type = multipart(
            [("batch_no", str(batch_no)), ("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_hash)],
            [("photos", name, payload, "image/jpeg") for name, payload in batch],
        )
        common["Content-Type"] = content_type
        status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/photos/", "POST", body, common)
        assert status == 200, raw
    document = None
    if with_document:
        document = document_bytes()
        body, content_type = multipart(
            [("batch_no", "0"), ("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_hash)],
            [("documents", "通知.pdf", document, "application/pdf")],
        )
        assert len(body) < 40 * 1024 * 1024
        common["Content-Type"] = content_type
        status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/documents/", "POST", body, common)
        assert status == 200, raw
    body, content_type = multipart(
        [("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_hash)], [],
    )
    common["Content-Type"] = content_type
    status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/complete/", "POST", body, common)
    assert status == 200, raw
    result = json.loads(raw)
    return result["submission_id"], photos, roster, document


def login(client):
    status, _, page = client.request("/admin/login/")
    assert status == 200
    token = csrf_token(page.decode("utf-8"))
    body = urllib.parse.urlencode({"username": ADMIN_USER, "password": ADMIN_PASSWORD, "csrfmiddlewaretoken": token}).encode()
    status, _, _ = client.request("/admin/login/", "POST", body, {"Content-Type": "application/x-www-form-urlencoded", "Referer": BASE_URL + "/admin/login/", "X-CSRFToken": token})
    assert status in {200, 302}


def approve(client, submission_id):
    status, _, page = client.request("/admin/submissions/")
    assert status == 200
    token = csrf_token(page.decode("utf-8"))
    body = urllib.parse.urlencode({"action": "approved", "csrfmiddlewaretoken": token}).encode()
    status, _, _ = client.request(f"/admin/submissions/{submission_id}/review/", "POST", body, {"Content-Type": "application/x-www-form-urlencoded", "Referer": BASE_URL + "/admin/submissions/", "X-CSRFToken": token})
    assert status in {200, 302}


def verify_archive(payload, expected_count, expected_hashes, expected_document_hash=None, require_all_categories=False):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.testzip() is None
        photo_names = [name for name in archive.namelist() if name.lower().endswith(".jpg")]
        assert len(photo_names) == expected_count
        for name in photo_names:
            leaf = name.rsplit("/", 1)[-1]
            assert leaf in expected_hashes
            data = archive.read(name)
            assert hashlib.sha256(data).hexdigest() == expected_hashes[leaf]
            assert archive.getinfo(name).CRC == __import__("binascii").crc32(data) & 0xFFFFFFFF
        stats = [name for name in archive.namelist() if name.endswith("学生统计表.xlsx")]
        assert stats
        assert "总统计表.xlsx" in archive.namelist() if expected_count > 20 else True
        for name in stats:
            book = load_workbook(io.BytesIO(archive.read(name)), data_only=True)
            assert isinstance(book["统计汇总"]["B2"].value, int)
            assert isinstance(book["统计汇总"]["B8"].value, int)
        if expected_document_hash is not None:
            docs = [name for name in archive.namelist() if name.endswith("通知.pdf")]
            assert docs and hashlib.sha256(archive.read(docs[0])).hexdigest() == expected_document_hash
        if require_all_categories:
            assert any("/走读/学生照片/" in name for name in photo_names)
            assert any("/半走读/学生照片/" in name for name in photo_names)
            assert any("/住宿/学生照片/" in name for name in photo_names)


def main():
    client = HttpClient()
    mixed_id, mixed_photos, mixed_roster, mixed_document = submit(client, "R2-MIX", "高一", MIXED_PHOTOS, "R2MIX", with_document=True, large=True)
    login(client)
    approve(client, mixed_id)
    mixed_submission = Submission.objects.get(pk=mixed_id)
    assert mixed_submission.students.count() == MIXED_PHOTOS
    assert mixed_submission.photos.count() == MIXED_PHOTOS
    assert mixed_submission.documents.count() == 1
    assert mixed_submission.roster_file.size == len(mixed_roster)
    document = mixed_submission.documents.get()
    assert document.size == len(mixed_document)
    assert hashlib.sha256(Path(document.file.path).read_bytes()).hexdigest() == hashlib.sha256(mixed_document).hexdigest()
    mixed_hashes = {name: digest for (name, _), digest in zip(mixed_photos, [hashlib.sha256(payload).hexdigest() for _, payload in mixed_photos])}
    status, _, mixed_zip = client.request("/admin/export.zip?school_year=2026&class_name=R2-MIX")
    assert status == 200
    verify_archive(mixed_zip, MIXED_PHOTOS, mixed_hashes, hashlib.sha256(mixed_document).hexdigest(), require_all_categories=True)

    grades = ["初一", "初二", "初三", "高一", "高二", "高三", "高一", "高二", "高三", "初一"]
    def concurrent_submit(index):
        local = HttpClient()
        return submit(local, f"R2-C{index:02d}", grades[index], 20, f"R2C{index:02d}")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(concurrent_submit, range(10)))
    for submission_id, _, _, _ in results:
        approve(client, submission_id)
    all_hashes = dict(mixed_hashes)
    for submission_id, photos, roster, _ in results:
        submission = Submission.objects.get(pk=submission_id)
        assert submission.status == "approved"
        assert submission.students.count() == 20
        assert submission.photos.count() == 20
        assert submission.roster_file.size == len(roster)
        db_photo_hashes = list(StudentPhoto.objects.filter(submission=submission).values_list("original_name", "sha256"))
        assert len(db_photo_hashes) == 20
        for name, payload in photos:
            assert dict(db_photo_hashes)[name] == hashlib.sha256(payload).hexdigest()
            all_hashes[name] = hashlib.sha256(payload).hexdigest()
    status, _, all_zip = client.request("/admin/export.zip?school_year=2026")
    assert status == 200
    verify_archive(all_zip, MIXED_PHOTOS + 200, all_hashes, require_all_categories=True)
    assert Submission.objects.filter(status="approved").count() == 11
    assert Student.objects.filter(submission__status="approved").count() == 215
    print(f"HTTP_R2_FINAL_OK mixed=15+20MB-doc concurrent_classes=10 concurrent_photos=200 total_photos=215 zip_bytes={len(all_zip)}")


if __name__ == "__main__":
    main()
