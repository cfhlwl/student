from django.contrib import admin
from .models import AppSetting, AuditLog, ClassGroup, NamingRule, SchoolYear, Student, StudentPhoto, Submission, SubmissionDocument

for model in [SchoolYear, ClassGroup, Submission, Student, StudentPhoto, SubmissionDocument, NamingRule, AppSetting, AuditLog]:
    admin.site.register(model)

