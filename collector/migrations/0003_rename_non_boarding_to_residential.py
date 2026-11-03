from django.db import migrations


def rename_non_boarding_to_residential(apps, schema_editor):
    Student = apps.get_model("collector", "Student")
    Student.objects.filter(study_type="非走读").update(study_type="住宿")


def restore_residential_label(apps, schema_editor):
    Student = apps.get_model("collector", "Student")
    Student.objects.filter(study_type="住宿").update(study_type="非走读")


class Migration(migrations.Migration):
    dependencies = [("collector", "0002_alter_student_study_type")]
    operations = [migrations.RunPython(rename_non_boarding_to_residential, restore_residential_label)]

