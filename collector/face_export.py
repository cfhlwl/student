"""Prepare local, non-identifying face-device export photos.

This module only detects a single frontal face and produces a cropped JPEG. It
does not identify a person, create an embedding, or call an external service.
The original uploaded file is never modified.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from math import hypot
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
DEVICE_EXPORT_YUNET_PROFILE_VERSION = "face-device-yunet-v1"
DEVICE_FACE_DETECTOR_HAAR = "haar"
DEVICE_FACE_DETECTOR_YUNET = "yunet"
YUNET_MODEL_VERSION = "face_detection_yunet_2023mar"
YUNET_MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
YUNET_MODEL_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "models" / "face_detection_yunet" / "face_detection_yunet_2023mar.onnx"
YUNET_INPUT_SIZE = (320, 320)
YUNET_MAX_DETECTION_DIMENSION = 960
YUNET_SCORE_THRESHOLD = 0.70
YUNET_NMS_THRESHOLD = 0.30
YUNET_TOP_K = 5000
DEVICE_MAX_BYTES = 500 * 1024
DEVICE_MIN_JPEG_QUALITY = 70
DEVICE_JPEG_QUALITIES = (92, 88, 84, 80, 76, 72, DEVICE_MIN_JPEG_QUALITY)
DEVICE_FACE_MIN_PIXELS = 128
DEVICE_FACE_HEIGHT_RATIO = 1 / 3
DEVICE_DETECTION_BUDGET_SECONDS = 2.0
DEVICE_DETECTION_SLOT_WAIT_SECONDS = 5.0

_DETECTION_SLOT = threading.BoundedSemaphore(1)
_YUNET_DETECTION_SLOT = threading.BoundedSemaphore(1)
_YUNET_MODEL_CACHE_LOCK = threading.Lock()
_YUNET_MODEL_CACHE: dict[tuple[str, str, float, float], tuple[object, object]] = {}


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


@dataclass(frozen=True)
class YuNetDetection:
    box: tuple[int, int, int, int]
    landmarks: tuple[tuple[float, float], ...]
    score: float


def _error(code: str, message: str) -> DevicePhotoError:
    return DevicePhotoError(code, message)


def configured_face_detector() -> str:
    """Return the explicitly selected device detector.

    Haar remains the compatibility default. YuNet is opt-in until its isolated
    evaluation is complete; a missing or invalid configuration is reported as
    an error rather than silently changing detector families.
    """
    detector = os.getenv("FACE_DEVICE_DETECTOR", DEVICE_FACE_DETECTOR_HAAR).strip().lower()
    if detector not in {DEVICE_FACE_DETECTOR_HAAR, DEVICE_FACE_DETECTOR_YUNET}:
        raise DevicePhotoError("detector_config_invalid", f"设备人脸检测器配置无效：{detector or '空值'}。")
    return detector


def device_export_profile() -> dict[str, object]:
    """Return detector/model identity used by export fingerprints and reports."""
    detector = configured_face_detector()
    profile: dict[str, object] = {
        "detector": detector,
        "version": DEVICE_EXPORT_PROFILE_VERSION,
        "max_bytes": DEVICE_MAX_BYTES,
        "min_face_pixels": DEVICE_FACE_MIN_PIXELS,
        "min_face_height_ratio": DEVICE_FACE_HEIGHT_RATIO,
    }
    if detector == DEVICE_FACE_DETECTOR_YUNET:
        profile.update({
            "version": DEVICE_EXPORT_YUNET_PROFILE_VERSION,
            "model": YUNET_MODEL_VERSION,
            "model_sha256": os.getenv("FACE_YUNET_MODEL_SHA256", YUNET_MODEL_SHA256).lower(),
            "score_threshold": float(os.getenv("FACE_YUNET_SCORE_THRESHOLD", str(YUNET_SCORE_THRESHOLD))),
            "nms_threshold": float(os.getenv("FACE_YUNET_NMS_THRESHOLD", str(YUNET_NMS_THRESHOLD))),
        })
    return profile


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_yunet_detector(model_path: str, expected_sha256: str, score_threshold: float, nms_threshold: float):
    key = (str(model_path), expected_sha256.lower(), float(score_threshold), float(nms_threshold))
    with _YUNET_MODEL_CACHE_LOCK:
        cached = _YUNET_MODEL_CACHE.get(key)
        if cached is not None:
            return cached

        path = Path(model_path)
        if not path.is_file() or path.is_symlink():
            raise PhotoQualityModelUnavailable(f"YuNet 模型文件缺失：{path.name}")
        try:
            actual_sha256 = _sha256_file(path)
        except OSError as exc:
            raise PhotoQualityModelUnavailable(f"YuNet 模型文件无法读取：{exc}") from exc
        if actual_sha256.lower() != expected_sha256.lower():
            raise PhotoQualityModelUnavailable(
                f"YuNet 模型 SHA256 不匹配：期望 {expected_sha256}，实际 {actual_sha256}"
            )
        try:
            import cv2
            try:
                cv2.setNumThreads(1)
            except AttributeError:
                pass
            detector = cv2.FaceDetectorYN.create(
                str(path), "", YUNET_INPUT_SIZE,
                float(score_threshold), float(nms_threshold), YUNET_TOP_K,
            )
            if detector is None:
                raise RuntimeError("FaceDetectorYN.create 返回空检测器")
        except Exception as exc:
            raise PhotoQualityModelUnavailable(f"YuNet 模型加载失败：{exc}") from exc
        if len(_YUNET_MODEL_CACHE) >= 2:
            _YUNET_MODEL_CACHE.pop(next(iter(_YUNET_MODEL_CACHE)))
        result = (cv2, detector)
        _YUNET_MODEL_CACHE[key] = result
        return result


def _box_area(box: tuple[int, int, int, int]) -> int:
    return max(0, box[2]) * max(0, box[3])


def _same_face_detection(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    if not intersection:
        return False
    union = _box_area(first) + _box_area(second) - intersection
    if union <= 0:
        return False
    iou = intersection / union
    first_center = (first[0] + first[2] / 2, first[1] + first[3] / 2)
    second_center = (second[0] + second[2] / 2, second[1] + second[3] / 2)
    center_distance = hypot(first_center[0] - second_center[0], first_center[1] - second_center[1])
    center_limit = 0.35 * min(first[2], first[3], second[2], second[3])
    width_ratio = max(first[2], second[2]) / min(first[2], second[2])
    height_ratio = max(first[3], second[3]) / min(first[3], second[3])
    return iou >= 0.65 and center_distance <= center_limit and width_ratio <= 1.35 and height_ratio <= 1.35


def _dedupe_boxes(raw_boxes) -> list[tuple[int, int, int, int]]:
    boxes: list[tuple[int, int, int, int]] = []
    normalized = []
    for raw_box in raw_boxes:
        box = tuple(int(value) for value in raw_box[:4])
        if box[2] <= 0 or box[3] <= 0:
            continue
        normalized.append(box)
    for box in sorted(normalized, key=lambda item: (-_box_area(item), item)):
        if any(_same_face_detection(box, existing) for existing in boxes):
            continue
        boxes.append(box)
    return boxes


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


def _detect_face_haar(image: Image.Image) -> tuple[int, int, int, int, float, float, float, float, float]:
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
    frontal_boxes = _dedupe_boxes(faces)
    profile_boxes = _dedupe_boxes(profile_faces)
    if not frontal_boxes and profile_boxes:
        raise _error("profile_face", "检测到明显侧脸，不能生成设备专用照片。")
    if not frontal_boxes:
        raise _error("no_face", "未检测到清晰正脸，不能生成设备专用照片。")
    if len(frontal_boxes) > 1 or any(
        not any(_same_face_detection(profile_box, frontal_box) for frontal_box in frontal_boxes)
        for profile_box in profile_boxes
    ):
        raise _error("multiple_faces", "检测到多张人脸，不能自动选择其中一张。")

    x, y, detected_width, detected_height = frontal_boxes[0]
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


def _same_yunet_detection(first: YuNetDetection, second: YuNetDetection) -> bool:
    if not _same_face_detection(first.box, second.box):
        return False
    first_nose = first.landmarks[2]
    second_nose = second.landmarks[2]
    width = min(first.box[2], second.box[2])
    height = min(first.box[3], second.box[3])
    return hypot(first_nose[0] - second_nose[0], first_nose[1] - second_nose[1]) <= 0.20 * min(width, height)


def _dedupe_yunet_detections(detections: list[YuNetDetection]) -> list[YuNetDetection]:
    unique: list[YuNetDetection] = []
    for detection in sorted(detections, key=lambda item: (-item.score, item.box, item.landmarks)):
        if any(_same_yunet_detection(detection, existing) for existing in unique):
            continue
        unique.append(detection)
    return unique


def _landmarks_are_frontal(detection: YuNetDetection) -> bool:
    left_eye, right_eye, nose, left_mouth, right_mouth = detection.landmarks
    _, _, width, height = detection.box
    eye_distance = hypot(left_eye[0] - right_eye[0], left_eye[1] - right_eye[1])
    eye_mid_x = (left_eye[0] + right_eye[0]) / 2
    mouth_mid_x = (left_mouth[0] + right_mouth[0]) / 2
    if eye_distance < width * 0.12:
        return False
    if abs(left_eye[1] - right_eye[1]) > height * 0.35:
        return False
    if abs(nose[0] - eye_mid_x) > width * 0.25:
        return False
    if abs(mouth_mid_x - eye_mid_x) > width * 0.30:
        return False
    if not (min(left_eye[1], right_eye[1]) <= nose[1] <= max(left_mouth[1], right_mouth[1])):
        return False
    return True


def _detect_face_yunet(image: Image.Image) -> tuple[int, int, int, int, float, float, float, float, float]:
    source_width, source_height = image.size
    scale = min(1.0, YUNET_MAX_DETECTION_DIMENSION / max(source_width, source_height))
    detection_width = max(1, round(source_width * scale))
    detection_height = max(1, round(source_height * scale))
    started = time.perf_counter()
    expected_sha256 = os.getenv("FACE_YUNET_MODEL_SHA256", YUNET_MODEL_SHA256)
    model_path = os.getenv("FACE_YUNET_MODEL_PATH", str(YUNET_MODEL_DEFAULT_PATH))
    score_threshold = float(os.getenv("FACE_YUNET_SCORE_THRESHOLD", str(YUNET_SCORE_THRESHOLD)))
    nms_threshold = float(os.getenv("FACE_YUNET_NMS_THRESHOLD", str(YUNET_NMS_THRESHOLD)))
    try:
        import cv2
        if scale != 1.0:
            detection_image = image.resize((detection_width, detection_height), Image.Resampling.LANCZOS)
        else:
            detection_image = image
        if detection_image.mode != "RGB":
            detection_image = detection_image.convert("RGB")
        rgb = np.asarray(detection_image)
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        if not _YUNET_DETECTION_SLOT.acquire(timeout=DEVICE_DETECTION_SLOT_WAIT_SECONDS):
            raise _error("detection_resource_busy", "YuNet 人脸检测资源繁忙，请稍后重试。")
        try:
            cv2_module, detector = _load_yunet_detector(model_path, expected_sha256, score_threshold, nms_threshold)
            detector.setInputSize((detection_width, detection_height))
            _, raw_detections = detector.detect(bgr)
        finally:
            _YUNET_DETECTION_SLOT.release()
    except DevicePhotoError:
        raise
    except PhotoQualityModelUnavailable as exc:
        raise _error("model_unavailable", str(exc)) from exc
    except Exception as exc:
        raise _error("detection_error", f"YuNet 图像检测失败：{exc}") from exc

    if time.perf_counter() - started > DEVICE_DETECTION_BUDGET_SECONDS:
        raise _error("detection_timeout", "YuNet 人脸检测超过软时间预算，未导出该照片。")
    detections: list[YuNetDetection] = []
    if raw_detections is not None:
        for row in np.asarray(raw_detections):
            if len(row) < 15 or float(row[14]) < score_threshold:
                continue
            box = tuple(int(round(float(value))) for value in row[:4])
            if box[2] <= 0 or box[3] <= 0:
                continue
            landmarks = tuple(
                (float(row[index]), float(row[index + 1]))
                for index in (4, 6, 8, 10, 12)
            )
            detections.append(YuNetDetection(box=box, landmarks=landmarks, score=float(row[14])))
    detections = _dedupe_yunet_detections(detections)
    if not detections:
        raise _error("no_face", "YuNet 未检测到清晰正脸，不能生成设备专用照片。")
    if len(detections) > 1:
        raise _error("multiple_faces", "YuNet 检测到多张人脸，不能自动选择其中一张。")
    detection = detections[0]
    if not _landmarks_are_frontal(detection):
        raise _error("occluded_or_side_face", "YuNet 关键点显示面部存在遮挡或明显侧脸。")

    x, y, detected_width, detected_height = detection.box
    scale_x = source_width / detection_width
    scale_y = source_height / detection_height
    x = round(x * scale_x)
    y = round(y * scale_y)
    face_width = round(detected_width * scale_x)
    face_height = round(detected_height * scale_y)
    if face_width < DEVICE_FACE_MIN_PIXELS or face_height < DEVICE_FACE_MIN_PIXELS:
        raise _error("face_too_small", f"人脸区域为 {face_width}×{face_height} 像素，小于 {DEVICE_FACE_MIN_PIXELS}×{DEVICE_FACE_MIN_PIXELS} 像素。")
    brightness = float(gray.mean())
    clipped_ratio = float(((gray <= 10) | (gray >= 245)).mean())
    sharpness = float(cv2_module.Laplacian(gray, cv2_module.CV_64F).var())
    if sharpness < MIN_SHARPNESS:
        raise _error("blurry", f"图像清晰度不足（清晰度值 {sharpness:.1f}）。")
    if brightness < MIN_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness < 128:
        raise _error("underexposed", "图像偏暗或存在明显欠曝。")
    if brightness > MAX_BRIGHTNESS or clipped_ratio > MAX_CLIPPED_RATIO and brightness >= 128:
        raise _error("overexposed", "图像偏亮或存在明显过曝。")
    return x, y, face_width, face_height, source_width, source_height, sharpness, brightness, clipped_ratio


def _detect_face(image: Image.Image) -> tuple[int, int, int, int, float, float, float, float, float]:
    detector = configured_face_detector()
    if detector == DEVICE_FACE_DETECTOR_YUNET:
        return _detect_face_yunet(image)
    return _detect_face_haar(image)


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


def _save_jpeg_under_limit(
    image_to_save: Image.Image,
    face_width: int,
    face_height: int,
    output_dir: Path,
    *,
    source_size: tuple[int, int] | None = None,
) -> tuple[Path, DevicePhotoMetrics]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="device-photo-", suffix=".jpg", dir=output_dir)
    os.close(fd)
    path = Path(name)
    source_width, source_height = source_size or image_to_save.size
    try:
        for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
            width = max(1, round(image_to_save.width * scale))
            height = max(1, round(image_to_save.height * scale))
            scaled_face_width = round(face_width * scale)
            scaled_face_height = round(face_height * scale)
            if scaled_face_width < DEVICE_FACE_MIN_PIXELS or scaled_face_height < DEVICE_FACE_MIN_PIXELS:
                break
            image = image_to_save if scale == 1.0 else image_to_save.resize((width, height), Image.Resampling.LANCZOS)
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
                        source_width, source_height, scaled_face_width, scaled_face_height,
                        width, height, size, quality,
                    )
        raise _error("compression_target_unmet", "在保持合理清晰度和最低人脸尺寸的情况下，无法达到 500KB 试行目标。")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _copy_original_jpeg(source_path: Path, output_dir: Path, image_size: tuple[int, int], face_size: tuple[int, int]) -> tuple[Path, DevicePhotoMetrics]:
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="device-photo-", suffix=".jpg", dir=output_dir)
    os.close(fd)
    path = Path(name)
    try:
        shutil.copyfile(source_path, path)
        with Image.open(path) as check:
            if check.format != "JPEG" or check.size != image_size or check.mode != "RGB":
                raise _error("jpeg_validation", "设备专用 JPEG 直通校验失败。")
        return path, DevicePhotoMetrics(
            image_size[0], image_size[1], face_size[0], face_size[1],
            image_size[0], image_size[1], path.stat().st_size, 0,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise


def prepare_device_photo(source_path: str | Path, output_dir: str | Path) -> tuple[Path, DevicePhotoMetrics]:
    """Create one device JPEG from a SHA-verified staged source file."""
    source_path = Path(source_path)
    try:
        with Image.open(source_path) as source:
            _validate_source(source)
            source_format = source.format
            source_mode = source.mode
            source_orientation = source.getexif().get(274)
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.load()
    except DevicePhotoError:
        raise
    except (FileNotFoundError, OSError, UnidentifiedImageError, ValueError) as exc:
        raise _error("invalid_image", f"图片损坏或无法读取：{exc}") from exc
    try:
        x, y, face_width, face_height, *_ = _detect_face(image)
        image_size = image.size
        face_ratio = face_height / max(1, image.height)
        if face_ratio >= DEVICE_FACE_HEIGHT_RATIO:
            if (
                source_format == "JPEG"
                and source_mode == "RGB"
                and source_orientation in (None, 1)
                and source_path.stat().st_size <= DEVICE_MAX_BYTES
            ):
                return _copy_original_jpeg(source_path, Path(output_dir), image_size, (face_width, face_height))
            return _save_jpeg_under_limit(
                image, face_width, face_height, Path(output_dir), source_size=image_size,
            )
        cropped, _ = _crop_head_shoulders(image, x, y, face_width, face_height)
        return _save_jpeg_under_limit(
            cropped, face_width, face_height, Path(output_dir), source_size=image_size,
        )
    except DevicePhotoError:
        raise
    except Exception as exc:
        raise _error("device_photo_error", f"设备专用照片处理失败：{exc}") from exc
