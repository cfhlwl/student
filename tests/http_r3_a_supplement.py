"""R3-A supplement flow over the real Nginx HTTP endpoint."""

import hashlib
import json

import http_r2_final as matrix
from collector.models import PhotoSupplement, StudentPhoto


def csrf_page(client, path):
    status, _, page = client.request(path)
    assert status == 200
    return matrix.csrf_token(page.decode("utf-8"))


def main():
    client = matrix.HttpClient()
    base = matrix.BASE_URL
    csrf = csrf_page(client, "/submit")
    photos = [(f"R3SUP{index:02d}.jpg", matrix.small_photo(index)) for index in range(4)]
    manifest = [{"name": name, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in photos]
    roster = matrix.roster_bytes("R3-SUP", "高一", 5, "R3SUP")
    roster_hash = hashlib.sha256(roster).hexdigest()
    body, content_type = matrix.multipart(
        [("expected_photo_count", "4"), ("expected_document_count", "0"), ("photo_manifest", json.dumps(manifest, ensure_ascii=False)), ("roster_fingerprint", roster_hash), ("note", "R3-A补传HTTP")],
        [("roster", "名单.xlsx", roster, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")],
    )
    headers = {"Content-Type": content_type, "X-CSRFToken": csrf, "Referer": base + "/submit"}
    status, _, raw = client.request("/submit/upload/start/", "POST", body, headers)
    assert status == 200, raw
    state = json.loads(raw)
    upload_id = state["upload_id"]
    upload_token = state["upload_token"]
    body, content_type = matrix.multipart(
        [("batch_no", "0"), ("upload_token", upload_token), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_hash)],
        [("photos", name, payload, "image/jpeg") for name, payload in photos],
    )
    headers["Content-Type"] = content_type
    status, _, raw = client.request(f"/submit/upload/{upload_id}/photos/", "POST", body, headers)
    assert status == 200, raw
    body, content_type = matrix.multipart(
        [("upload_token", upload_token), ("manifest_digest", state["photo_manifest_digest"]), ("roster_fingerprint", roster_hash)], [],
    )
    headers["Content-Type"] = content_type
    status, _, raw = client.request(f"/submit/upload/{upload_id}/complete/", "POST", body, headers)
    assert status == 200, raw
    result = json.loads(raw)
    submission_id = result["submission_id"]
    result_token = result["token"]

    matrix.login(client)
    matrix.approve(client, submission_id)
    missing_name = "R3SUP04.jpg"
    missing = matrix.small_photo(77)
    result_csrf = csrf_page(client, f"/submit/result/{submission_id}/?token={result_token}")
    body, content_type = matrix.multipart(
        [("csrfmiddlewaretoken", result_csrf), ("token", result_token)],
        [("photos", missing_name, missing, "image/jpeg")],
    )
    status, _, raw = client.request(
        f"/submit/result/{submission_id}/supplement/",
        "POST",
        body,
        {"Content-Type": content_type, "X-CSRFToken": result_csrf, "Referer": base + f"/submit/result/{submission_id}/?token={result_token}"},
    )
    assert status in {200, 302, 303}, (status, raw[:500])
    supplement = PhotoSupplement.objects.get(submission_id=submission_id, status="pending")

    admin_csrf = csrf_page(client, "/admin/submissions/")
    body, content_type = matrix.multipart([("csrfmiddlewaretoken", admin_csrf), ("action", "approved")], [])
    status, _, raw = client.request(
        f"/admin/supplements/{supplement.id}/review/",
        "POST",
        body,
        {"Content-Type": content_type, "X-CSRFToken": admin_csrf, "Referer": base + "/admin/submissions/"},
    )
    assert status in {200, 302, 303}, (status, raw[:500])
    assert StudentPhoto.objects.filter(submission_id=submission_id).count() == 5
    status, _, archive = client.request("/admin/export.zip?school_year=2026&class_name=R3-SUP")
    assert status == 200, archive[:500]
    expected_hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in photos}
    expected_hashes[missing_name] = hashlib.sha256(missing).hexdigest()
    matrix.verify_archive(archive, 5, expected_hashes, require_all_categories=True, require_total=False)
    print(f"HTTP_R3_SUPPLEMENT_OK submission={submission_id} pending_to_approved=1 photos=5 zip_bytes={len(archive)}")


if __name__ == "__main__":
    main()
