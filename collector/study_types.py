import re


STUDY_TYPE_VALUES = ("走读", "半走读", "住宿")


def normalize_study_type(value):
    """Normalize common roster labels to the stored study-type contract."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    compact = re.sub(r"[\s\u3000\uFEFF]+", "", text)
    compact = re.sub(r"[，,。；;：:、/\\|_\-()（）\[\]【】]+", "", compact)
    if "非走读" in compact or "不走读" in compact:
        raise ValueError("不支持非走读或不走读")

    if "半走读" in compact:
        if "住宿" in compact:
            raise ValueError("就读类型同时包含多个类别")
        return "半走读"
    if "住宿" in compact:
        if "走读" in compact:
            raise ValueError("就读类型同时包含多个类别")
        return "住宿"
    if "走读" in compact:
        return "走读"
    raise ValueError("就读类型不正确")
