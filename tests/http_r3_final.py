"""R3 final real-HTTP 300 x ~3MB preflight/upload evidence."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django

django.setup()

from openpyxl import Workbook, load_workbook  # noqa: E402
from collector.models import StudentPhoto, Submission  # noqa: E402
from tests.http_r2_final import HttpClient, csrf_token, multipart, roster_bytes, large_photo, login, approve, split_batches  # noqa: E402


BASE_URL = os.getenv("R3F_BASE_URL", "http://nginx").rstrip("/")
ADMIN_PASSWORD = os.environ["R3F_ADMIN_PASSWORD"]


def roster_bytes_3digit(class_name, grade, count, prefix):
    department = "初中部" if grade.startswith("初") else "高中部"
    book = Workbook()
    sheet = book.active
    sheet.append(["姓名", "性别", "学部", "学年", "年级", "班级", "就读类型", "备注"])
    for index in range(count):
        sheet.append([f"{prefix}{index:03d}", "男" if index % 2 else "女", department, "2026", grade, class_name, ["走读", "半走读", "住宿"][index % 3], "R3-FINAL虚构数据"])
    output = io.BytesIO()
    book.save(output)
    return output.getvalue()


def main():
    client = HttpClient()
    count = 300
    prefix = "R3BIG"
    temporary = Path(tempfile.mkdtemp(prefix="r3-final-300-"))
    try:
        status, _, page = client.request("/submit")
        assert status == 200
        csrf = csrf_token(page.decode("utf-8"))
        roster = roster_bytes_3digit("R3-300-BIG2", "高一", count, prefix)
        roster_hash = hashlib.sha256(roster).hexdigest()
        manifest = []
        photo_paths = []
        for index in range(count):
            payload = large_photo(index)
            path = temporary / f"{prefix}{index:03d}.jpg"
            path.write_bytes(payload)
            photo_paths.append(path)
            manifest.append({"name": path.name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        photos = [(item["name"], path.read_bytes()) for item, path in zip(manifest, photo_paths)]
        batches = split_batches(photos)
        assert len(batches) >= 15

        # Each preflight request is itself bounded by the same 20/32 MiB split.
        preflight_photos = 0
        for batch in batches:
            body, content_type = multipart(
                [],
                [("roster", "名单.xlsx", roster, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")]
                + [("photos", name, payload, "image/jpeg") for name, payload in batch],
            )
            status, _, raw = client.request(
                "/submit/preflight/check/", "POST", body,
                {"Content-Type": content_type, "X-CSRFToken": csrf, "Referer": BASE_URL + "/submit/preflight/"},
            )
            assert status == 200, raw[:500]
            preflight_photos += len(json.loads(raw).get("photos", []))
        assert preflight_photos == count

        common = {"X-CSRFToken": csrf, "Referer": BASE_URL + "/submit"}
        start_fields = [
            ("expected_photo_count", str(count)),
            ("expected_document_count", "0"),
            ("photo_manifest", json.dumps(manifest, ensure_ascii=False)),
            ("roster_fingerprint", roster_hash),
            ("note", "R3-FINAL 300x3MB 虚构数据"),
        ]
        body, content_type = multipart(start_fields, [("roster", "名单.xlsx", roster, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")])
        common["Content-Type"] = content_type
        status, _, raw = client.request("/submit/upload/start/", "POST", body, common)
        assert status == 200, raw[:500]
        state = json.loads(raw)
        for batch_no, batch in enumerate(batches):
            fields = [("batch_no", str(batch_no)), ("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_hash)]
            body, content_type = multipart(fields, [("photos", name, payload, "image/jpeg") for name, payload in batch])
            common["Content-Type"] = content_type
            status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/photos/", "POST", body, common)
            assert status == 200, raw[:500]
        body, content_type = multipart([("upload_token", state["upload_token"]), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_hash)], [])
        common["Content-Type"] = content_type
        status, _, raw = client.request(f"/submit/upload/{state['upload_id']}/complete/", "POST", body, common)
        assert status == 200, raw[:500]
        submission_id = json.loads(raw)["submission_id"]
        login(client)
        approve(client, submission_id)
        submission = Submission.objects.get(pk=submission_id)
        assert submission.status == "approved"
        assert StudentPhoto.objects.filter(submission=submission).count() == count
        expected = {item["name"]: item["sha256"] for item in manifest}
        for photo in StudentPhoto.objects.filter(submission=submission):
            assert photo.sha256 == expected[photo.original_name]
            assert hashlib.sha256(Path(photo.file.path).read_bytes()).hexdigest() == expected[photo.original_name]
        status, _, archive_bytes = client.request("/admin/export.zip?school_year=2026&class_name=R3-300-BIG2")
        assert status == 200
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            assert archive.testzip() is None
            names = [name for name in archive.namelist() if name.lower().endswith(".jpg")]
            assert len(names) == count
            for name in names:
                data = archive.read(name)
                assert archive.getinfo(name).CRC == __import__("binascii").crc32(data) & 0xFFFFFFFF
            stats = [name for name in archive.namelist() if name.endswith("学生统计表.xlsx")]
            assert stats
            assert sum(load_workbook(io.BytesIO(archive.read(name)), data_only=True)["统计汇总"]["B8"].value for name in stats) == count
        print(f"HTTP_R3_FINAL_300X3_OK preflight_photos={preflight_photos} upload_photos={count} batches={len(batches)} zip_photos={len(names)} zip_bytes={len(archive_bytes)}")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    main()
