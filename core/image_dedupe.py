"""参考图片去重工具。临时 URL 不同也以实际文件内容为准。"""
import hashlib
from pathlib import Path


def image_content_digest(path: str | Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
