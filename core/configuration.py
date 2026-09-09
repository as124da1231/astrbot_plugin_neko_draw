"""Configuration validation at the WebUI boundary, without changing schema keys.

旧配置继承策略：保存时尽量保持用户数据原貌——
- 不在保存时写回 __template_key / provider，缺失标识由前端渲染层与运行时按模板兜底；
- template_list 内的嵌套字段采用宽容规整：能安全转换则转换，否则保留原值，绝不因
  旧数据的字段瑕疵而拒绝整体保存；
- schema 中不存在的遗留键原样保留，兼容跨版本更新；
- 对顶层关键数值保持严格正数校验，并拒绝重复的模型模板名与预设触发词。
"""
from copy import deepcopy
import math


MODEL_TEMPLATE_PROVIDERS = {
    'seedream_text': 'wavespeed',
    'seedream_edit': 'wavespeed',
    'runninghub_text': 'runninghub',
    'runninghub_edit': 'runninghub',
    'openapi_text': 'openapi',
}


def _coerce(value, field, path, strict):
    """按字段类型规整值。strict=True 时类型不符抛错；否则宽容保留原值。"""
    kind = field.get('type')
    if kind in ('int', 'float'):
        if isinstance(value, bool):
            if strict:
                raise ValueError(f'{path} 必须是数字')
            return value
        try:
            number = float(value)
            if not math.isfinite(number) or (kind == 'int' and not number.is_integer()):
                raise ValueError()
            return int(number) if kind == 'int' else number
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f'{path} 必须是有效的{"整数" if kind == "int" else "数字"}')
            return value
    if kind == 'bool':
        if not isinstance(value, bool) and strict:
            raise ValueError(f'{path} 必须是开关值')
        return value
    if kind in ('string', 'str'):
        if not isinstance(value, str) and strict:
            raise ValueError(f'{path} 必须是文本')
        return value
    if kind == 'list':
        invalid = not isinstance(value, list) or any(not isinstance(x, str) for x in value)
        if invalid and strict:
            raise ValueError(f'{path} 必须是文本列表')
        return value
    if kind == 'file':
        ok = isinstance(value, str) or (
            isinstance(value, list) and all(isinstance(x, str) for x in value)
        )
        if not ok and strict:
            raise ValueError(f'{path} 必须是文件路径')
        return value
    return value


def validate_config(config, schema):
    result = deepcopy(config)

    def validate(value, field, path, strict=True):
        kind = field.get('type')
        if kind == 'template_list':
            if not isinstance(value, list):
                if strict:
                    raise ValueError(f'{path} 必须是模板列表')
                return value
            templates = field.get('templates', {})
            fallback = next(iter(templates.values()), {})
            for index, item in enumerate(value):
                # 旧版字符串预设原样保留（运行时 PromptManager 负责兼容迁移）
                if path == 'prompt' and isinstance(item, str):
                    continue
                if not isinstance(item, dict):
                    # 无法识别的项宽容跳过，不阻断整体保存
                    continue
                # 用户主动保存（权威落盘点）：给历史遗留、缺失模板标识的对象项补回
                # __template_key，只增不改、保留其余全部字段。这样保存后的配置可被
                # AstrBot 原生“插件配置”页按 template_list 正确识别（改触发词等编辑后
                # 不会因缺标识而读取不到）。加载/启动阶段仍保持原样、不做此改写。
                if templates and not item.get('__template_key'):
                    item['__template_key'] = next(iter(templates))
                if path == 'model_templates' and item.get('__template_key') in MODEL_TEMPLATE_PROVIDERS:
                    item['provider'] = MODEL_TEMPLATE_PROVIDERS[item['__template_key']]
                template = templates.get(item.get('__template_key'), fallback)
                for key, spec in template.get('items', {}).items():
                    if key in item:
                        # 嵌套字段宽容规整：转换失败保留原值，不因旧数据瑕疵拒绝保存
                        item[key] = validate(
                            item[key], spec, f'{path}[{index}].{key}', strict=False
                        )
            return value
        return _coerce(value, field, path, strict)

    # 顶层字段：schema 内的按类型严格规整；遗留的未知键原样保留（跨版本兼容）
    for key, value in list(result.items()):
        if key in schema:
            result[key] = validate(value, schema[key], key)

    # 仅顶层关键数值保持严格正数校验
    for key in ('timeout', 'poll_interval', 'max_concurrency'):
        if key in result:
            number = result[key]
            if isinstance(number, bool) or not isinstance(number, (int, float)) or number <= 0:
                raise ValueError(f'{key} 必须大于 0')

    # 这些字段在运行时会被转换为以名称为键的字典。若允许重复，后面的项会静默
    # 覆盖前面的项，面板看到的配置与真正生效的配置就会不一致，因此在落盘前拒绝。
    model_names = []
    for item in result.get('model_templates', []) or []:
        if isinstance(item, dict):
            name = str(item.get('name', '')).strip()
            if name:
                model_names.append(name)
    duplicate_models = sorted({name for name in model_names if model_names.count(name) > 1})
    if duplicate_models:
        raise ValueError(f'模型模板名称必须唯一，重复项：{"、".join(duplicate_models)}')

    prompt_triggers = []
    for item in result.get('prompt', []) or []:
        if isinstance(item, dict):
            trigger = str(item.get('trigger', '')).strip()
        elif isinstance(item, str):
            trigger = item.strip().split(None, 1)[0] if item.strip() else ''
        else:
            trigger = ''
        if trigger:
            prompt_triggers.append(trigger)
    duplicate_triggers = sorted({trigger for trigger in prompt_triggers if prompt_triggers.count(trigger) > 1})
    if duplicate_triggers:
        raise ValueError(f'预设提示词触发词必须唯一，重复项：{"、".join(duplicate_triggers)}')
    return result
