"""Local, non-identifying photo quality checks for student uploads.

The checks intentionally return quality measurements only. They do not create
face embeddings, identify a person, or send the image to a third party.
"""

from __future__ import annotations

import time
import tracemalloc
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from PIL import Image, ImageOps


MIN_FACE_PIXELS = 128
MIN_SHARPNESS = 80.0
MIN_BRIGHTNESS = 45.0
MAX_BRIGHTNESS = 210.0
MAX_CLIPPED_RATIO = 0.02
MAX_IMAGE_PIXELS = 24_000_000
MAX_DETECTION_DIMENSION = 1600
MAX_DETECTION_SECONDS = 2.0
PHOTO_QUALITY_POLICY_VERSION = "haar-v2-20261111"


class PhotoQualityPolicyError(RuntimeError):
    """The enforcement policy could not be read safely."""


class PhotoQualityModelUnavailable(RuntimeError):
    """OpenCV or one of its bundled classifiers is unavailable."""


@dataclass(frozen=True)
class PhotoQualityPolicy:
    enabled: bool
    version: str = PHOTO_QUALITY_POLICY_VERSION
    configured: bool = False


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
    elapsed_ms: float | None = None
    cpu_time_ms: float | None = None
    peak_memory_bytes: int | None = None
    detection_width: int = 0
    detection_height: int = 0

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "code": self.code, "message": self.message,
            "width": self.width, "height": self.height, "face_count": self.face_count,
            "face_width": self.face_width, "face_height": self.face_height,
            "sharpness": round(self.sharpness, 2) if self.sharpness is not None else None,
            "brightness": round(self.brightness, 2) if self.brightness is not None else None,
            "clipped_ratio": round(self.clipped_ratio, 4) if self.clipped_ratio is not None else None,
            "elapsed_ms": round(self.elapsed_ms, 2) if self.elapsed_ms is not None else None,
            "cpu_time_ms": round(self.cpu_time_ms, 2) if self.cpu_time_ms is not None else None,
            "peak_memory_bytes": self.peak_memory_bytes,
            "detection_width": self.detection_width, "detection_height": self.detection_height,
        }


def read_face_quality_policy() -> PhotoQualityPolicy:
    """Read the policy without failing open on a database error."""
    try:
        from .models import AppSetting
        setting = AppSetting.objects.filter(key="photo_face_quality_blocking").first()
    except Exception as exc:
        raise PhotoQualityPolicyError("照片质量拦截策略读取失败，请稍后重试") from exc
    if setting is None:
        return PhotoQualityPolicy(enabled=False)
    return PhotoQualityPolicy(enabled=setting.value.lower() in {"1", "true", "yes", "on"}, configured=True)


def face_quality_blocking_enabled() -> bool:
    return read_face_quality_policy().enabled


@lru_cache(maxsize=1)
def _load_classifiers():
    try:
        import cv2
        base = cv2.data.haarcascades
        frontal = cv2.CascadeClassifier(base + "haarcascade_frontalface_default.xml")
        profile = cv2.CascadeClassifier(base + "haarcascade_profileface.xml")
        eyes = cv2.CascadeClassifier(base + "haarcascade_eye_tree_eyeglasses.xml")
        if frontal.empty() or profile.empty() or eyes.empty():
            raise RuntimeError("人脸质量检测模型加载失败")
        return cv2, frontal, profile, eyes
    except Exception as exc:
        raise PhotoQualityModelUnavailable(f"人脸质量检测模型不可用：{exc}") from exc


def _rewind(upload: Any) -> None:
    if hasattr(upload, "seek"):
        upload.seek(0)


def _finish_metrics(start_wall: float, start_cpu: float, started_tracing: bool) -> tuple[float, float, int | None]:
    elapsed_ms = (time.perf_counter() - start_wall) * 1000
    cpu_time_ms = (time.process_time() - start_cpu) * 1000
    peak = None
    if tracemalloc.is_tracing():
        _, peak = tracemalloc.get_traced_memory()
        if started_tracing:
            tracemalloc.stop()
    return elapsed_ms, cpu_time_ms, peak


def _result(status: str, code: str, message: str, *, start_wall: float, start_cpu: float,
            started_tracing: bool, **kwargs: Any) -> PhotoQualityResult:
    elapsed_ms, cpu_time_ms, peak = _finish_metrics(start_wall, start_cpu, started_tracing)
    return PhotoQualityResult(status, code, message, elapsed_ms=elapsed_ms, cpu_time_ms=cpu_time_ms,
                              peak_memory_bytes=peak, **kwargs)


def assess_photo_quality(upload: Any, *, min_face_pixels: int = MIN_FACE_PIXELS) -> PhotoQualityResult:
    """Assess one image with bounded, aspect-preserving OpenCV work."""
    start_wall = time.perf_counter()
    start_cpu = time.process_time()
    started_tracing = not tracemalloc.is_tracing()
    if started_tracing:
        tracemalloc.start()
    try:
        import numpy as np
    except Exception as exc:
        _rewind(upload)
        return _result("model_unavailable", "opencv_unavailable", f"人脸质量检测模型不可用：{exc}", start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
    try:
        _rewind(upload)
        with Image.open(upload) as source:
            source_width, source_height = source.size
            if source_width <= 0 or source_height <= 0:
                return _result("detection_error", "invalid_image_dimensions", "图片尺寸无效。", start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
            if source_width * source_height > MAX_IMAGE_PIXELS:
                return _result("detection_error", "image_pixel_budget", f"图片像素超过检测预算（最多 {MAX_IMAGE_PIXELS:,} 像素）。", width=source_width, height=source_height, start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
            image = ImageOps.exif_transpose(source)
            width, height = image.size
            if width <= 0 or height <= 0:
                return _result("detection_error", "invalid_image_dimensions", "图片尺寸无效。", start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
            if width * height > MAX_IMAGE_PIXELS:
                return _result("detection_error", "image_pixel_budget", f"图片像素超过检测预算（最多 {MAX_IMAGE_PIXELS:,} 像素）。", width=width, height=height, start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
            scale = min(1.0, MAX_DETECTION_DIMENSION / max(width, height))
            detection_width = max(1, round(width * scale))
            detection_height = max(1, round(height * scale))
            gray_image = image.convert("L")
            if scale != 1.0:
                gray_image = gray_image.resize((detection_width, detection_height), Image.Resampling.LANCZOS)
            gray = np.asarray(gray_image)
        try:
            cv2, frontal, profile, eyes = _load_classifiers()
        except PhotoQualityModelUnavailable:
            raise
        try:
            faces = frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
            profile_faces = profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
            brightness = float(gray.mean())
            clipped_ratio = float(((gray <= 10) | (gray >= 245)).mean())
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except Exception as exc:
            raise RuntimeError(f"图像检测失败：{exc}") from exc
        if time.perf_counter() - start_wall > MAX_DETECTION_SECONDS:
            return _result("detection_error", "detection_timeout", f"人脸质量检测超过 {MAX_DETECTION_SECONDS:.1f} 秒预算。", width=width, height=height, sharpness=sharpness, brightness=brightness, clipped_ratio=clipped_ratio, detection_width=detection_width, detection_height=detection_height, start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
        common = dict(width=width, height=height, sharpness=sharpness, brightness=brightness, clipped_ratio=clipped_ratio, detection_width=detection_width, detection_height=detection_height)
        if len(faces) == 0 and len(profile_faces) > 0:
            return _result("failed", "profile_face", "检测到明显侧脸，请重新上传正脸照片。", face_count=1, start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing, **common)
        if len(faces) == 0:
            return _result("failed", "no_face", "未检测到清晰正脸，请重新上传单人正脸照片。", start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing, **common)
        if len(faces) > 1:
            return _result("failed", "multiple_faces", "检测到多张人脸，请上传单人照片。", face_count=len(faces), start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing, **common)
        x, y, detected_width, detected_height = [int(value) for value in faces[0]]
        scale_x = width / detection_width
        scale_y = height / detection_height
        face_width = round(detected_width * scale_x)
        face_height = round(detected_height * scale_y)
        issues: list[tuple[str, str]] = []
        if face_width < min_face_pixels or face_height < min_face_pixels:
            issues.append(("face_too_small", f"人脸区域为 {face_width}×{face_height} 像素，小于 {min_face_pixels}×{min_face_pixels} 像素。"))
        if face_height * 3 < height:
            issues.append(("face_frame_too_small", "人脸在画面中的纵向占比小于 1/3，请重新取景或上传更近的正脸照片。"))
        face_gray = gray[max(0, y):y + detected_height, max(0, x):x + detected_width]
        if face_gray.size == 0:
            raise RuntimeError("检测到的人脸区域为空")
        detected_eyes = eyes.detectMultiScale(face_gray, scaleFactor=1.1, minNeighbors=5, minSize=(max(10, detected_width // 10), max(10, detected_height // 10)))
        if len(detected_eyes) < 2:
            issues.append(("occluded_face", "疑似存在遮挡、明显侧脸或面部关键区域不可见。"))
        if sharpness < MIN_SHARPNESS:
            issues.append(("blurry", f"图像清晰度不足（清晰度值 {sharpness:.1f}，建议不低于 {MIN_SHARPNESS:.0f}）。"))
        if brightness < MIN_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness < 128:
            issues.append(("underexposed", "图像偏暗或存在明显欠曝，请补充均匀正面光线。"))
        elif brightness > MAX_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness >= 128:
            issues.append(("overexposed", "图像偏亮或存在明显过曝，请避免强光直射和面部反光。"))
        common.update(face_count=1, face_width=face_width, face_height=face_height)
        if issues:
            code, message = issues[0]
            if len(issues) > 1:
                message += "".join(f"；{item[1]}" for item in issues[1:])
            return _result("failed", code, message, start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing, **common)
        return _result("passed", "", "合格", start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing, **common)
    except PhotoQualityModelUnavailable as exc:
        _rewind(upload)
        return _result("model_unavailable", "opencv_unavailable", str(exc), start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
    except Exception as exc:
        _rewind(upload)
        return _result("detection_error", "quality_detection_error", f"图像质量检测失败：{exc}", start_wall=start_wall, start_cpu=start_cpu, started_tracing=started_tracing)
    finally:
        _rewind(upload)


def quality_issue_level(result: PhotoQualityResult) -> str:
    """Return the UI severity; policy read failures intentionally propagate."""
    return "error" if read_face_quality_policy().enabled else "warning"
