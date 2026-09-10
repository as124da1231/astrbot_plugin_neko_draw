"""图片服务协议识别与地址规则。

识别必须依赖明确的服务商特征，不能用通用版本路径（例如 /api/v3）猜测，
否则火山方舟等服务会被误判为 WaveSpeed。
"""


def infer_provider_type(base_url: object) -> str:
    value = str(base_url or "").strip().lower()
    if "wavespeed" in value:
        return "wavespeed"
    if "runninghub" in value:
        return "runninghub"
    return "openai"


def normalize_provider_base_url(base_url: object, protocol: str | None = None) -> str:
    value = str(base_url or "").strip().rstrip("/")
    selected = protocol or infer_provider_type(value)
    lower = value.lower()
    marker = "/api/v3" if selected == "wavespeed" else "/openapi/v2"
    if selected in {"wavespeed", "runninghub"} and marker in lower:
        return value[: lower.index(marker) + len(marker)]
    return value
