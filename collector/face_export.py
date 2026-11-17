"""Prepare local, non-identifying face-device export photos.

This module only detects a single frontal face and produces a cropped JPEG. It
does not identify a person, create an embedding, or call an external service.
The original uploaded file is never modified.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from .photo_quality import (
    MAX_DECODE_MEMORY_BYTES,
    MAX_DETECTION_DIMENSION,
    MAX_IMAGE_PIXELS,
    MAX_CLIPPED_RATIO,
    MAX_BRIGHTNESS,
    MIN_BRIGHTNESS,
    MIN_SHARPNESS,
    PhotoQualityModelUnavailable,
    _load_classifiers,
)


DEVICE_EXPORT_PROFILE_VERSION = "face-device-v1"
DEVICE_MAX_BYTES = 500 * 1024
DEVICE_MIN_JPEG_QUALITY = 70
DEVICE_JPEG_QUALITIES = (92, 88, 84, 80, 76, 72, DEVICE_MIN_JPEG_QUALITY)
DEVICE_FACE_MIN_PIXELS = 128
DEVICE_FACE_HEIGHT_RATIO = 1 / 3
DEVICE_DETECTION_BUDGET_SECONDS = 2.0
DEVICE_DETECTION_SLOT_WAIT_SECONDS = 5.0

_DETECTION_SLOT = threading.BoundedSemaphore(1)


class DevicePhotoError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DevicePhotoMetrics:
    source_width: int
    source_height: int
    face_width: int
    face_height: int
    output_width: int
    output_height: int
    output_size: int
    jpeg_quality: int


def _error(code: str, message: str) -> DevicePhotoError:
    return DevicePhotoError(code, message)


def _validate_source(image: Image.Image) -> None:
    if image.format not in {"JPEG", "PNG", "MPO"}:
        raise _error("unsupported_image", "图片格式不支持设备专用导出。")
    width, height = image.size
    if width <= 0 or height <= 0:
        raise _error("invalid_dimensions", "图片尺寸无效。")
    if width * height > MAX_IMAGE_PIXELS:
        raise _error("image_pixel_budget", f"图片像素超过检测预算（最多 {MAX_IMAGE_PIXELS:,} 像素）。")
    if width * height * 4 > MAX_DECODE_MEMORY_BYTES:
        raise _error("image_memory_budget", "图片解码内存超过设备导出预算。")


def _detect_face(image: Image.Image) -> tuple[int, int, int, int, float, float, float, float, float]:
    source_width, source_height = image.size
    scale = min(1.0, MAX_DETECTION_DIMENSION / max(source_width, source_height))
    detection_width = max(1, round(source_width * scale))
    detection_height = max(1, round(source_height * scale))
    gray_image = image.convert("L")
    if scale != 1.0:
        gray_image = gray_image.resize((detection_width, detection_height), Image.Resampling.LANCZOS)
    gray = np.asarray(gray_image)
    started = time.perf_counter()
    if not _DETECTION_SLOT.acquire(timeout=DEVICE_DETECTION_SLOT_WAIT_SECONDS):
        raise _error("detection_resource_busy", "设备专用人脸检测资源繁忙，请稍后重试。")
    try:
        try:
            cv2, frontal, profile, eyes = _load_classifiers()
        except PhotoQualityModelUnavailable as exc:
            raise _error("model_unavailable", str(exc)) from exc
        try:
            faces = frontal.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
            profile_faces = profile.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(32, 32))
            brightness = float(gray.mean())
            clipped_ratio = float(((gray <= 10) | (gray >= 245)).mean())
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        except Exception as exc:
            raise _error("detection_error", f"图像检测失败：{exc}") from exc
    finally:
        _DETECTION_SLOT.release()
    if time.perf_counter() - started > DEVICE_DETECTION_BUDGET_SECONDS:
        raise _error("detection_timeout", "设备专用人脸检测超过软时间预算，未导出该照片。")
    if len(faces) == 0 and len(profile_faces) > 0:
        raise _error("profile_face", "检测到明显侧脸，不能生成设备专用照片。")
    if len(faces) == 0:
        raise _error("no_face", "未检测到清晰正脸，不能生成设备专用照片。")
    if len(faces) > 1:
        raise _error("multiple_faces", "检测到多张人脸，不能自动选择其中一张。")

    x, y, detected_width, detected_height = [int(value) for value in faces[0]]
    scale_x = source_width / detection_width
    scale_y = source_height / detection_height
    x = round(x * scale_x)
    y = round(y * scale_y)
    face_width = round(detected_width * scale_x)
    face_height = round(detected_height * scale_y)
    if face_width < DEVICE_FACE_MIN_PIXELS or face_height < DEVICE_FACE_MIN_PIXELS:
        raise _error("face_too_small", f"人脸区域为 {face_width}×{face_height} 像素，小于 {DEVICE_FACE_MIN_PIXELS}×{DEVICE_FACE_MIN_PIXELS} 像素。")
    face_gray = gray[max(0, int(y / scale_y)):int((y + face_height) / scale_y), max(0, int(x / scale_x)):int((x + face_width) / scale_x)]
    detected_eyes = eyes.detectMultiScale(
        face_gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(max(10, detected_width // 10), max(10, detected_height // 10)),
    )
    if len(detected_eyes) < 2:
        raise _error("occluded_or_side_face", "面部关键区域不可见或疑似明显侧脸，不能生成设备专用照片。")
    if sharpness < MIN_SHARPNESS:
        raise _error("blurry", f"图像清晰度不足（清晰度值 {sharpness:.1f}）。")
    if brightness < MIN_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness < 128:
        raise _error("underexposed", "图像偏暗或存在明显欠曝。")
    if brightness > MAX_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness >= 128:
        raise _error("overexposed", "图像偏亮或存在明显过曝。")
    return x, y, face_width, face_height, source_width, source_height, sharpness, brightness, clipped_ratio


def _crop_head_shoulders(image: Image.Image, x: int, y: int, face_width: int, face_height: int) -> tuple[Image.Image, float]:
    width, height = image.size
    crop_width = min(width, max(int(face_width * 2.4), int(face_height * 1.6)))
    crop_height = min(height, max(int(face_height * 2.8), face_height + 2))
    left = x + face_width // 2 - crop_width // 2
    top = y - int(face_height * 0.55)
    left = max(0, min(left, width - crop_width))
    top = max(0, min(top, height - crop_height))
    right = left + crop_width
    bottom = top + crop_height
    cropped = image.crop((left, top, right, bottom))
    ratio = face_height / max(1, crop_height)
    if ratio < DEVICE_FACE_HEIGHT_RATIO:
        raise _error("face_height_ratio", "裁剪后人脸纵向占比不足 1/3。")
    return cropped, ratio


def _save_jpeg_under_limit(cropped: Image.Image, face_width: int, face_height: int, output_dir: Path) -> tuple[Path, DevicePhotoMetrics]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="device-photo-", suffix=".jpg", dir=output_dir)
    os.close(fd)
    path = Path(name)
    try:
        for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
            width = max(1, round(cropped.width * scale))
            height = max(1, round(cropped.height * scale))
            scaled_face_width = round(face_width * scale)
            scaled_face_height = round(face_height * scale)
            if scaled_face_width < DEVICE_FACE_MIN_PIXELS or scaled_face_height < DEVICE_FACE_MIN_PIXELS:
                break
            image = cropped if scale == 1.0 else cropped.resize((width, height), Image.Resampling.LANCZOS)
            for quality in DEVICE_JPEG_QUALITIES:
                image.save(path, format="JPEG", quality=quality, optimize=False, progressive=False, subsampling=0)
                size = path.stat().st_size
                if size <= DEVICE_MAX_BYTES:
                    with Image.open(path) as check:
                        if check.format != "JPEG" or check.size != (width, height):
                            raise _error("jpeg_validation", "设备专用 JPEG 校验失败。")
                    if scaled_face_height / height < DEVICE_FACE_HEIGHT_RATIO:
                        raise _error("face_height_ratio", "压缩后人脸纵向占比不足 1/3。")
                    return path, DevicePhotoMetrics(
                        cropped.width, cropped.height, scaled_face_width, scaled_face_height,
                        width, height, size, quality,
                    )
        raise _error("compression_target_unmet", "在保持合理清晰度和最低人脸尺寸的情况下，无法达到 500KB 试行目标。")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def prepare_device_photo(source_path: str | Path, output_dir: str | Path) -> tuple[Path, DevicePhotoMetrics]:
    """Create one device JPEG from a SHA-verified staged source file."""
    source_path = Path(source_path)
    try:
        with Image.open(source_path) as source:
            _validate_source(source)
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.load()
    except DevicePhotoError:
        raise
    except (FileNotFoundError, OSError, UnidentifiedImageError, ValueError) as exc:
        raise _error("invalid_image", f"图片损坏或无法读取：{exc}") from exc
    try:
        x, y, face_width, face_height, *_ = _detect_face(image)
        cropped, _ = _crop_head_shoulders(image, x, y, face_width, face_height)
        return _save_jpeg_under_limit(cropped, face_width, face_height, Path(output_dir))
    except DevicePhotoError:
        raise
    except Exception as exc:
        raise _error("device_photo_error", f"设备专用照片处理失败：{exc}") from exc
