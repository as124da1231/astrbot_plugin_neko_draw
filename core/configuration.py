"""WebUI 配置的类型、唯一性与引用完整性校验。

旧配置继承策略：保存时尽量保持用户数据原貌——
- 不在保存时写回 __template_key / provider，缺失标识由前端渲染层与运行时按模板兜底；
- template_list 内的嵌套字段采用宽容规整：能安全转换则转换，否则保留原值，绝不因
  旧数据的字段瑕疵而拒绝整体保存；
- schema 中不存在的遗留键原样保留，兼容跨版本更新；
- 对顶层关键数值保持严格正数校验，并拒绝重复的模型模板名与预设触发词。
"""
from copy import deepcopy
import math


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

    minimum = int(result.get('apng_maker_min_frames', 2))
    maximum = int(result.get('apng_maker_max_frames', 20))
    if minimum < 1 or maximum < 1 or minimum > maximum:
        raise ValueError('APNG 最低图片数必须至少为 1，且不能大于最大帧数')
    if maximum > 100:
        raise ValueError('APNG 最大帧数不能超过 100')
    default_interval = float(result.get('apng_default_interval_seconds', 5))
    if not 0.1 <= default_interval <= 60:
        raise ValueError('APNG 默认间隔秒数必须为 0.1～60')
    for key, label in (('drawing_history_limit', '生图历史保存上限'), ('apng_history_limit', 'APNG 历史保存上限')):
        if int(result.get(key, 200)) < 0:
            raise ValueError(f'{label}不能小于 0')

    # v2 将提供商、模型和模型参数放在同一层。先校验权威结构，再投影给稳定运行时。
    integrated = result.get('image_providers')
    if isinstance(integrated, list):
        integrated_names = []
        integrated_models = []
        available_models_by_mode = {'text': set(), 'edit': set()}
        for p_index, provider in enumerate(integrated):
            if not isinstance(provider, dict):
                raise ValueError(f'模型提供商[{p_index + 1}]格式无效')
            name = str(provider.get('name', '')).strip()
            provider.pop('protocol', None)
            if not name:
                raise ValueError(f'模型提供商[{p_index + 1}]名称不能为空')
            integrated_names.append(name)
            source = str(provider.get('source', 'custom')).lower()
            if source not in {'custom', 'astrbot'}:
                raise ValueError(f'模型提供商「{name}」来源无效')
            if source == 'astrbot' and not str(provider.get('astrbot_provider_id', '')).strip():
                raise ValueError(f'模型提供商「{name}」必须选择 AstrBot 提供商')
            if source == 'custom':
                base_url = str(provider.get('base_url', '')).strip()
                if not base_url.startswith(('http://', 'https://')):
                    raise ValueError(f'模型提供商「{name}」的 API Base URL 无效')
            local_names = []
            for m_index, model in enumerate(provider.get('models', []) or []):
                if not isinstance(model, dict):
                    raise ValueError(f'模型提供商「{name}」的模型[{m_index + 1}]格式无效')
                alias = str(model.get('name') or model.get('model') or '').strip()
                model_id = str(model.get('model', '')).strip()
                if not alias or not model_id:
                    raise ValueError(f'模型提供商「{name}」的模型名称和模型 ID 不能为空')
                local_names.append(alias)
                integrated_models.append(alias)
                model_mode = str(model.get('mode', 'text')).lower()
                if model_mode not in {'text', 'edit'}:
                    raise ValueError(f'模型「{alias}」的用途必须是文生图或图片编辑')
                if provider.get('enabled', True) and model.get('enabled', True) and model.get('enabled_as_default', True):
                    available_models_by_mode[model_mode].add(alias)
            duplicate_local = sorted({n for n in local_names if local_names.count(n) > 1})
            if duplicate_local:
                raise ValueError(f'提供商「{name}」内模型名称重复：{"、".join(duplicate_local)}')
        duplicate_integrated = sorted({n for n in integrated_names if integrated_names.count(n) > 1})
        if duplicate_integrated:
            raise ValueError(f'模型提供商名称必须唯一，重复项：{"、".join(duplicate_integrated)}')
        duplicate_models_v2 = sorted({n for n in integrated_models if integrated_models.count(n) > 1})
        if duplicate_models_v2:
            raise ValueError(f'模型显示名称必须全局唯一，重复项：{"、".join(duplicate_models_v2)}')
        for key, mode in (('default_text_model_v2', 'text'), ('default_edit_model_v2', 'edit')):
            selected = str(result.get(key, '')).strip()
            if selected and not selected.startswith('@astrbot:') and selected not in available_models_by_mode[mode]:
                result[key] = ''
        from .providers import sync_image_provider_config
        sync_image_provider_config(result)

    # 这些字段在运行时会被转换为以名称为键的字典。若允许重复，后面的项会静默
    # 覆盖前面的项，面板看到的配置与真正生效的配置就会不一致，因此在落盘前拒绝。
    provider_names = []
    valid_provider_types = {'wavespeed', 'runninghub', 'openai', 'openapi', 'astrbot'}
    for index, item in enumerate(result.get('model_providers', []) or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get('name', '')).strip()
        provider_type = str(item.get('type', '')).strip().lower()
        if not name:
            raise ValueError(f'模型提供商[{index + 1}]名称不能为空')
        if provider_type not in valid_provider_types:
            raise ValueError(f'模型提供商「{name}」类型无效')
        if provider_type == 'astrbot' and not str(item.get('astrbot_provider_id', '')).strip():
            raise ValueError(f'模型提供商「{name}」必须选择 AstrBot 模型提供商')
        if provider_type == 'astrbot' and str(item.get('astrbot_protocol', 'auto')).strip().lower() not in {
            'auto', 'wavespeed', 'runninghub', 'openai', 'openapi'
        }:
            raise ValueError(f'模型提供商「{name}」的图像接口协议无效')
        provider_names.append(name)
    duplicate_providers = sorted({name for name in provider_names if provider_names.count(name) > 1})
    if duplicate_providers:
        raise ValueError(f'模型提供商名称必须唯一，重复项：{"、".join(duplicate_providers)}')

    model_names = []
    for item in result.get('model_templates', []) or []:
        if isinstance(item, dict):
            name = str(item.get('name', '')).strip()
            if name:
                model_names.append(name)
            provider = str(item.get('provider', '')).strip()
            if not provider:
                raise ValueError(f'模型模板「{name or "未命名"}」必须选择模型提供商')
            if provider not in provider_names:
                raise ValueError(f'模型模板「{name or "未命名"}」选择的提供商「{provider}」不存在')
    duplicate_models = sorted({name for name in model_names if model_names.count(name) > 1})
    if duplicate_models:
        raise ValueError(f'模型模板名称必须唯一，重复项：{"、".join(duplicate_models)}')

    for key, label in (
        ('default_text_model', '默认文生图模板'),
        ('default_edit_model', '默认编辑模板'),
    ):
        selected = str(result.get(key, '')).strip()
        if selected and selected not in model_names:
            raise ValueError(f'{label}「{selected}」不存在')

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
