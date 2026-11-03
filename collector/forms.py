from django import forms


class MultipleFileInput(forms.FileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        single_clean = super().clean
        if not data:
            return []
        if isinstance(data, (list, tuple)):
            return [single_clean(item, initial) for item in data]
        return [single_clean(data, initial)]


class SubmissionForm(forms.Form):
    roster = forms.FileField(label="Excel 学生名单", help_text="仅支持 .xlsx，建议使用系统模板")
    photos = MultipleFileField(label="学生照片", required=False, widget=MultipleFileInput(attrs={"accept": ".jpg,.jpeg,.png"}))
    documents = MultipleFileField(label="相关文档", required=False, widget=MultipleFileInput(attrs={"accept": ".pdf,.doc,.docx,.xls,.xlsx"}))
    note = forms.CharField(label="提交说明", required=False, max_length=500, widget=forms.Textarea(attrs={"rows": 2, "placeholder": "可选，例如：高一新生资料第一批"}))

    def clean_roster(self):
        upload = self.cleaned_data["roster"]
        if not upload.name.lower().endswith(".xlsx"):
            raise forms.ValidationError("名单必须是 .xlsx 文件")
        return upload


class NamingRuleForm(forms.Form):
    mode = forms.ChoiceField(choices=[("name", "模式 A · 仅姓名"), ("gender_no", "模式 B · 姓名 + 性别编号"), ("custom", "模式 C · 自定义字段组合")])
    template = forms.CharField(max_length=200, label="命名模板", help_text="可用字段：{姓名} {性别编号} {学部} {班级} {就读类型}")
    male_code = forms.CharField(max_length=10, label="男生编号", initial="01")
    female_code = forms.CharField(max_length=10, label="女生编号", initial="02")
    separator = forms.CharField(max_length=5, required=False, label="姓名与编号分隔符")


class ExportFilterForm(forms.Form):
    school_year = forms.CharField(required=False, label="学年")
    department = forms.ChoiceField(required=False, choices=[("", "全部学部"), ("middle", "初中部"), ("high", "高中部")], label="学部")
    class_name = forms.CharField(required=False, label="班级")
    gender = forms.ChoiceField(required=False, choices=[("", "全部性别"), ("男", "男"), ("女", "女")], label="性别")
    study_type = forms.ChoiceField(required=False, choices=[("", "全部类别"), ("走读", "走读"), ("半走读", "半走读"), ("住宿", "住宿")], label="就读类型")
    has_photo = forms.ChoiceField(required=False, choices=[("", "全部照片状态"), ("yes", "有照片"), ("no", "缺照片")], label="照片状态")

