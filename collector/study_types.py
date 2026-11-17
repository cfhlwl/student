import re


STUDY_TYPE_VALUES = ("走读", "半走读", "住宿")


def normalize_study_type(value):
    """Accept only the explicit study-type contract and approved suffixes."""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    compact = re.sub(r"[\s\u3000\uFEFF]+", "", text)
    if any(marker in compact for marker in ("不", "非", "无", "未")):
        raise ValueError("就读类型不能包含否定或不确定表述")
    aliases = {
        "走读": "走读",
        "走读生": "走读",
        "半走读": "半走读",
        "半走读生": "半走读",
        "住宿": "住宿",
        "住宿生": "住宿",
    }
    try:
        return aliases[compact]
    except KeyError as exc:
        raise ValueError("就读类型必须填写走读、住宿或半走读（可加“生”）") from exc
