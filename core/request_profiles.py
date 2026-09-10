"""服务商/模型特有的请求兼容层。

通用流程只负责构造语义参数；这里集中处理服务商实际端点和字段差异，
避免在消息处理器、模型路由和 HTTP 客户端之间散落特判。
"""
from typing import Any


def normalize_openai_image_request(
    base_url: str, model: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    body = dict(payload)
    endpoint = str(body.pop("_endpoint", "images/generations")).strip("/")
    url = str(base_url or "").lower()
    model_id = str(model or "").lower()

    # SiliconFlow 的 Qwen Image Edit 仍使用 images/generations，且参考图为
    # 单数 image 字段。兼容旧版本曾自动写入的 images/edits + images 配置。
    if "siliconflow" in url and "qwen" in model_id and "image-edit" in model_id:
        endpoint = "images/generations"
        if "image" not in body and "images" in body:
            images = body.pop("images")
            body["image"] = images[0] if isinstance(images, list) and images else images

    return endpoint, body
