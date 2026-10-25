"""Bounded uploads; storage paths never come from a client-supplied directory."""
import base64
import binascii
import io
import uuid
from pathlib import Path

from PIL import Image

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_KEYS = {'reference_image', 'drawing_first_frame_path', 'apng_first_frame_path'}
FORMATS = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp', 'GIF': '.gif', 'BMP': '.bmp'}


def _store_image_bytes(data_dir, key, raw: bytes) -> dict:
    """校验并写入图片，返回相对/绝对路径（不信任客户端提供的目录）。"""
    if not raw or len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError('图片为空或超过 20 MB')
    try:
        with Image.open(io.BytesIO(raw)) as img:
            ext = FORMATS.get(img.format)
            img.verify()
        if ext is None:
            raise ValueError('不支持的图片格式')
    except Exception as exc:
        raise ValueError('请上传有效的 PNG/JPEG/WebP/GIF/BMP 图片') from exc
    root = (Path(data_dir) / 'files').resolve()
    target = (root / key / (uuid.uuid4().hex + ext)).resolve()
    if not target.is_relative_to(root):
        raise ValueError('上传路径越界')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return {'path': target.relative_to(Path(data_dir).resolve()).as_posix(), 'absolute_path': str(target)}


def store_upload_bytes(data_dir, config_key, raw: bytes) -> dict:
    """multipart 上传入口：按字段名校验后落盘。"""
    key = str(config_key or '').strip()
    if key not in ALLOWED_KEYS:
        raise ValueError('无效的上传配置字段')
    return _store_image_bytes(data_dir, key, bytes(raw))


def store_upload(data_dir, body):
    """兼容旧的 base64 JSON 上传入口。"""
    key = body.get('config_key')
    if key not in ALLOWED_KEYS:
        raise ValueError('无效的上传配置字段')
    encoded = body.get('data', '')
    if not isinstance(encoded, str):
        raise ValueError('无效的文件数据')
    encoded = encoded.split(',', 1)[-1]
    if len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
        raise ValueError('图片不能超过 20 MB')
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('base64 解码失败') from exc
    return _store_image_bytes(data_dir, key, raw)
