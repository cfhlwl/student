from django.urls import path

from .password_views import admin_password_change

from .views import (
    StaffLoginView, StaffLogoutView, admin_classes, admin_dashboard, admin_naming, admin_statistics, admin_statistics_exceptions, admin_students,
    admin_submissions, clear_test_data, download_template, export_zip, manual_match, manual_supplement_match, protected_document, protected_photo, public_submit, review_submission, review_supplement, submission_preview, submit_preflight, submit_preflight_page, submit_result, submit_supplement, toggle_photo_quality, toggle_upload, upload_complete, upload_document_batch, upload_photo_batch, upload_start, upload_status,
)

urlpatterns = [
    path("submit", public_submit, name="submit"),
    path("submit/preflight/", submit_preflight_page, name="submit-preflight-page"),
    path("submit/preflight/check/", submit_preflight, name="submit-preflight"),
    path("submit/upload/start/", upload_start, name="upload-start"),
    path("submit/upload/<uuid:session_id>/photos/", upload_photo_batch, name="upload-photo-batch"),
    path("submit/upload/<uuid:session_id>/documents/", upload_document_batch, name="upload-document-batch"),
    path("submit/upload/<uuid:session_id>/complete/", upload_complete, name="upload-complete"),
    path("submit/upload/<uuid:session_id>/status/", upload_status, name="upload-status"),
    path("submit/template.xlsx", download_template, name="submit-template"),
    path("submit/result/<uuid:submission_id>/", submit_result, name="submit-result"),
    path("submit/result/<uuid:submission_id>/supplement/", submit_supplement, name="submit-supplement"),
    path("admin/login/", StaffLoginView.as_view(), name="admin-login"),
    path("admin/logout/", StaffLogoutView.as_view(), name="admin-logout"),
    path("admin/password/change/", admin_password_change, name="admin-password-change"),
    path("admin/", admin_dashboard, name="admin-dashboard"),
    path("admin/statistics/", admin_statistics, name="admin-statistics"),
    path("admin/statistics/exceptions.xlsx", admin_statistics_exceptions, name="admin-statistics-exceptions"),
    path("admin/submissions/", admin_submissions, name="admin-submissions"),
    path("admin/submissions/<uuid:submission_id>/preview/", submission_preview, name="submission-preview"),
    path("admin/submissions/<uuid:submission_id>/review/", review_submission, name="review-submission"),
    path("admin/supplements/<uuid:supplement_id>/review/", review_supplement, name="review-supplement"),
    path("admin/students/", admin_students, name="admin-students"),
    path("admin/students/photos/<int:photo_id>/match/", manual_match, name="manual-match"),
    path("admin/supplement-photos/<int:photo_id>/match/", manual_supplement_match, name="manual-supplement-match"),
    path("admin/files/photos/<int:photo_id>/", protected_photo, name="protected-photo"),
    path("admin/files/documents/<int:document_id>/", protected_document, name="protected-document"),
    path("admin/export.zip", export_zip, name="export-zip"),
    path("admin/naming/", admin_naming, name="admin-naming"),
    path("admin/classes/", admin_classes, name="admin-classes"),
    path("admin/settings/upload/", toggle_upload, name="toggle-upload"),
    path("admin/settings/photo-quality/", toggle_photo_quality, name="toggle-photo-quality"),
    path("admin/settings/clear-test-data/", clear_test_data, name="clear-test-data"),
]

