"""Local, non-identifying photo quality checks for student uploads.

The checks intentionally return quality measurements only.  They do not create
face embeddings, identify a person, or send the image to a third party.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageOps


MIN_FACE_PIXELS = 128
MIN_SHARPNESS = 80.0
MIN_BRIGHTNESS = 45.0
MAX_BRIGHTNESS = 210.0
MAX_CLIPPED_RATIO = 0.02


@dataclass(frozen=True)
class PhotoQualityResult:
    status: str
    code: str
    message: str
    width: int = 0
    height: int = 0
    face_count: int = 0
    face_width: int = 0
    face_height: int = 0
    sharpness: float | None = None
    brightness: float | None = None
    clipped_ratio: float | None = None

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "width": self.width,
            "height": self.height,
            "face_count": self.face_count,
            "face_width": self.face_width,
            "face_height": self.face_height,
            "sharpness": round(self.sharpness, 2) if self.sharpness is not None else None,
            "brightness": round(self.brightness, 2) if self.brightness is not None else None,
            "clipped_ratio": round(self.clipped_ratio, 4) if self.clipped_ratio is not None else None,
        }


def face_quality_blocking_enabled() -> bool:
    try:
        from .models import AppSetting

        return AppSetting.get_bool("photo_face_quality_blocking", False)
    except Exception:
        return False


@lru_cache(maxsize=1)
def _load_classifiers():
    import cv2

    base = cv2.data.haarcascades
    frontal = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
    profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")
    eyes = cv2.CascadeClassifier(base + "haarcascade_eye_tree_eyeglasses.xml")
    if frontal.empty() or profile.empty() or eyes.empty():
        raise RuntimeError("人脸质量检测模型加载失败")
    return cv2, frontal, profile, eyes


def _rewind(upload: Any) -> None:
    if hasattr(upload, "seek"):
        upload.seek(0)


def assess_photo_quality(upload: Any, *, min_face_pixels: int = MIN_FACE_PIXELS) -> PhotoQualityResult:
    """Assess one image without persisting or identifying its face."""

    try:
        import numpy as np

        cv2, frontal, profile, eyes = _load_classifiers()
        _rewind(upload)
        with Image.open(upload) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            width, height = image.size
            rgb = np.asarray(image)
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        faces = frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
        profile_faces = profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
        brightness = float(gray.mean())
        clipped_ratio = float(((gray <= 10) | (gray >= 245)).mean())
        sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    except Exception as exc:
        _rewind(upload)
        return PhotoQualityResult("unavailable", "quality_unavailable", f"人脸质量检测暂不可用：{exc}")
    finally:
        _rewind(upload)

    if len(faces) == 0 and len(profile_faces) > 0:
        return PhotoQualityResult(
            "failed", "profile_face", "检测到明显侧脸，请重新上传正脸照片。", width, height,
            face_count=1, sharpness=sharpness, brightness=brightness, clipped_ratio=clipped_ratio,
        )
    if len(faces) == 0:
        return PhotoQualityResult(
            "failed", "no_face", "未检测到清晰正脸，请重新上传单人正脸照片。", width, height,
            sharpness=sharpness, brightness=brightness, clipped_ratio=clipped_ratio,
        )
    if len(faces) > 1:
        return PhotoQualityResult(
            "failed", "multiple_faces", "检测到多张人脸，请上传单人照片。", width, height,
            face_count=len(faces), sharpness=sharpness, brightness=brightness, clipped_ratio=clipped_ratio,
        )

    x, y, face_width, face_height = [int(value) for value in faces[0]]
    issues: list[tuple[str, str]] = []
    if face_width < min_face_pixels or face_height < min_face_pixels:
        issues.append(("face_too_small", f"人脸区域为 {face_width}×{face_height} 像素，小于 {min_face_pixels}×{min_face_pixels} 像素。"))
    if face_height * 3 < height:
        issues.append(("face_frame_too_small", "人脸在画面中的纵向占比小于 1/3，请重新取景或上传更近的正脸照片。"))

    face_gray = gray[y:y + face_height, x:x + face_width]
    detected_eyes = eyes.detectMultiScale(face_gray, scaleFactor=1.1, minNeighbors=5, minSize=(max(10, face_width // 10), max(10, face_height // 10)))
    if len(detected_eyes) < 2:
        issues.append(("occluded_face", "疑似存在遮挡、明显侧脸或面部关键区域不可见。"))
    if sharpness < MIN_SHARPNESS:
        issues.append(("blurry", f"图像清晰度不足（清晰度值 {sharpness:.1f}，建议不低于 {MIN_SHARPNESS:.0f}）。"))
    if brightness < MIN_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness < 128:
        issues.append(("underexposed", "图像偏暗或存在明显欠曝，请补充均匀正面光线。"))
    elif brightness > MAX_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness >= 128:
        issues.append(("overexposed", "图像偏亮或存在明显过曝，请避免强光直射和面部反光。"))

    if issues:
        code, message = issues[0]
        if len(issues) > 1:
            message += "".join(f"；{item[1]}" for item in issues[1:])
        return PhotoQualityResult(
            "failed", code, message, width, height, face_count=1, face_width=face_width,
            face_height=face_height, sharpness=sharpness, brightness=brightness, clipped_ratio=clipped_ratio,
        )
    return PhotoQualityResult(
        "passed", "", "合格", width, height, face_count=1, face_width=face_width,
        face_height=face_height, sharpness=sharpness, brightness=brightness, clipped_ratio=clipped_ratio,
    )


def quality_issue_level(result: PhotoQualityResult) -> str:
    return "error" if face_quality_blocking_enabled() else "warning"
