from django.urls import path

from .views import (
    StaffLoginView, StaffLogoutView, admin_classes, admin_dashboard, admin_naming, admin_students,
    admin_submissions, clear_test_data, download_template, export_zip, manual_match, protected_document, protected_photo, public_submit, review_submission, submit_result, toggle_upload, upload_complete, upload_document_batch, upload_photo_batch, upload_start,
)

urlpatterns = [
    path("submit", public_submit, name="submit"),
    path("submit/upload/start/", upload_start, name="upload-start"),
    path("submit/upload/<uuid:session_id>/photos/", upload_photo_batch, name="upload-photo-batch"),
    path("submit/upload/<uuid:session_id>/documents/", upload_document_batch, name="upload-document-batch"),
    path("submit/upload/<uuid:session_id>/complete/", upload_complete, name="upload-complete"),
    path("submit/template.xlsx", download_template, name="submit-template"),
    path("submit/result/<uuid:submission_id>/", submit_result, name="submit-result"),
    path("admin/login/", StaffLoginView.as_view(), name="admin-login"),
    path("admin/logout/", StaffLogoutView.as_view(), name="admin-logout"),
    path("admin/", admin_dashboard, name="admin-dashboard"),
    path("admin/submissions/", admin_submissions, name="admin-submissions"),
    path("admin/submissions/<uuid:submission_id>/review/", review_submission, name="review-submission"),
    path("admin/students/", admin_students, name="admin-students"),
    path("admin/students/photos/<int:photo_id>/match/", manual_match, name="manual-match"),
    path("admin/files/photos/<int:photo_id>/", protected_photo, name="protected-photo"),
    path("admin/files/documents/<int:document_id>/", protected_document, name="protected-document"),
    path("admin/export.zip", export_zip, name="export-zip"),
    path("admin/naming/", admin_naming, name="admin-naming"),
    path("admin/classes/", admin_classes, name="admin-classes"),
    path("admin/settings/upload/", toggle_upload, name="toggle-upload"),
    path("admin/settings/clear-test-data/", clear_test_data, name="clear-test-data"),
]

