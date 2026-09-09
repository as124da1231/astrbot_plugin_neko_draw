"""Offline regression coverage: python -m unittest discover -s tests -v."""
import asyncio
import base64
from copy import deepcopy
import importlib
import io
import inspect
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
def load(name):
    return importlib.import_module(f'{PKG}.{name}')

templates = load('core.templates')
provider_factory = load('core.providers')
parser = load('core.parser')
RateLimiter = load('core.rate_limit').RateLimiter
PromptManager = load('core.prompt_manager').PromptManager
WhitelistGuard = load('core.whitelist').WhitelistGuard
store_upload = load('core.uploads').store_upload
validate_config = load('core.configuration').validate_config
from PIL import Image as PILImage

class Config(dict):
    def save_config(self):
        self.saved = deepcopy(dict(self))

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_history_annotations_do_not_resolve_shadowed_list_eagerly(self):
        history = load('core.history')
        annotation = history.HistoryStore._delete_source_files.__annotations__["paths"]
        self.assertEqual(annotation, "list[str]")

    def test_model_template_uses_selected_provider_name(self):
        item = {'name': 'image_model', 'model': 'm', 'provider': 'OpenAI 主账号'}
        model = templates.TemplateManager([item]).get('image_model')
        self.assertEqual(model.provider, 'OpenAI 主账号')

    def test_multiple_providers_of_same_type(self):
        clients = provider_factory.create_providers({
            'model_providers': [
                {'name': 'OpenAI 主账号', 'type': 'OpenAI', 'api_key': 'a', 'base_url': 'https://one.example/v1'},
                {'name': 'OpenAI 备用', 'type': 'OpenAI', 'api_key': 'b', 'base_url': 'https://two.example/v1'},
            ],
        })
        self.assertEqual(set(clients), {'OpenAI 主账号', 'OpenAI 备用'})
        self.assertEqual(clients['OpenAI 主账号'].api_key, 'a')
        self.assertEqual(clients['OpenAI 备用'].base_url, 'https://two.example/v1')

    def test_astrbot_provider_reference_does_not_copy_credentials(self):
        context = types.SimpleNamespace(get_provider_by_id=lambda _provider_id: object())
        clients = provider_factory.create_providers({
            'model_providers': [{
                'name': 'AstrBot OpenAI', 'type': 'AstrBot',
                'astrbot_provider_id': 'openai-main',
            }],
        }, context=context)
        self.assertEqual(clients['AstrBot OpenAI'].provider_id, 'openai-main')

    def test_astrbot_provider_protocol_is_inferred_and_base_is_normalized(self):
        provider = types.SimpleNamespace(
            provider_config={
                'api_base': 'https://api.wavespeed.ai/api/v3/bytedance/seedream-v5.0-pro',
            },
            get_current_key=lambda: 'secret-from-astrbot',
        )
        context = types.SimpleNamespace(get_provider_by_id=lambda _provider_id: provider)
        protocol, key, base_url, _headers = provider_factory.resolve_astrbot_provider(
            context, 'wavespeed'
        )
        self.assertEqual(protocol, 'wavespeed')
        self.assertEqual(key, 'secret-from-astrbot')
        self.assertEqual(base_url, 'https://api.wavespeed.ai/api/v3')

    def test_default_fallback_uses_lowest_number_and_matching_mode(self):
        manager = templates.TemplateManager([
            {'name': 'text-late', 'model': 'a', 'fallback_order': 20, 'enabled_as_default': True},
            {'name': 'text-first', 'model': 'b', 'fallback_order': 2, 'enabled_as_default': True},
            {'name': 'edit', 'model': 'c', 'refer_field': 'images', 'fallback_order': 0, 'enabled_as_default': True},
        ])
        self.assertEqual(manager.fallback(False).name, 'text-first')
        self.assertEqual(manager.fallback(True).name, 'edit')

    def test_legacy_keys_migrate_to_provider_cards(self):
        config = {
            'api_key': 'wave-key',
            'runninghub_api_key': 'rh-key',
            'openapi_api_key': 'open-key',
            'model_templates': [{'name': 'legacy', 'model': 'x', 'provider': 'openapi'}],
        }
        provider_factory.ensure_provider_config(config)
        self.assertEqual([item['name'] for item in config['model_providers']], ['WaveSpeed', 'RunningHub', 'OpenAI'])
        self.assertEqual(config['model_providers'][2]['api_key'], 'open-key')
        self.assertEqual(config['model_templates'][0]['provider'], 'OpenAI')
        self.assertEqual(config['image_providers'][2]['models'][0]['name'], 'legacy')

    def test_integrated_provider_projects_to_runtime(self):
        config = {
            'image_providers': [{
                'name': '硅基流动', 'source': 'custom', 'protocol': 'OpenAI',
                'api_key': 'secret', 'base_url': 'https://api.siliconflow.cn/v1',
                'enabled': True, 'models': [{
                    'name': 'Qwen-Image', 'model': 'Qwen/Qwen-Image', 'mode': 'text',
                    'enabled': True, 'fallback_order': 0, 'params': {'size': '1024x1024'},
                }],
            }],
            'default_text_model_v2': 'Qwen-Image', 'default_edit_model_v2': '',
            '_image_provider_config_migrated': True,
        }
        provider_factory.sync_image_provider_config(config)
        self.assertEqual(config['model_providers'][0]['name'], '硅基流动')
        self.assertEqual(config['model_templates'][0]['provider'], '硅基流动')
        self.assertEqual(config['model_templates'][0]['fallback_order'], 0)
        self.assertEqual(config['default_text_model'], 'Qwen-Image')

    def test_astrbot_default_creates_virtual_image_model(self):
        config = {
            'image_providers': [], '_image_provider_config_migrated': True,
            'default_text_model_v2': '@astrbot:siliconflow:Qwen/Qwen-Image',
            'default_edit_model_v2': '',
        }
        provider_factory.sync_image_provider_config(config)
        self.assertEqual(config['model_providers'][0]['astrbot_provider_id'], 'siliconflow')
        self.assertEqual(config['model_templates'][0]['model'], 'Qwen/Qwen-Image')
        self.assertEqual(config['default_text_model'], '@astrbot:siliconflow:Qwen/Qwen-Image')

    def test_structured_payload(self):
        values = {'nested': {'a': [1, None]}, 'arr': [1, 2], 'empty': None, 'flag': True}
        t = templates.ModelTemplate('x', 'm', params=values)
        self.assertEqual(templates.build_payload('cat', {}, t), {**values, 'prompt': 'cat'})

    def test_siliconflow_images_response_is_extracted(self):
        client = load('core.openapi').OpenAPIClient
        self.assertEqual(
            client._extract_urls({'images': [{'url': 'https://cdn.example/result.png'}]}),
            ['https://cdn.example/result.png'],
        )

    def test_generated_image_mime_can_be_detected_without_content_type(self):
        downloader = load('core.downloader').Downloader
        self.assertEqual(downloader._detect_image_mime(b'\x89PNG\r\n\x1a\nrest'), 'image/png')

    def test_quoted_preset(self):
        p = parser.parse_prompt_message('nd cat', ['nd {{user_text}} --negative_prompt "bad quality"'])
        self.assertEqual(p.text, 'cat')
        self.assertEqual(p.params['negative_prompt'], 'bad quality')

    def test_empty_template_no_duplicate_user_text(self):
        self.assertEqual(parser.parse_prompt_message('draw cat', ['draw']).text, 'cat')

    def test_user_param_override(self):
        p = parser.parse_prompt_message('nd cat --n 2', ['nd {{user_text}} --n 1'])
        self.assertEqual(p.params['n'], '2')

    def test_limit_pending_release_success_and_expiry(self):
        now = [0]
        r = RateLimiter({'enable_rate_limit': True, 'rate_limit_rules': [{'window_seconds': 60, 'max_count': 1}]}, lambda: now[0])
        token, rule, _ = r.reserve('u')
        self.assertIsNone(rule)
        for _ in range(10):
            self.assertIsNotNone(r.reserve('u')[1])
        now[0] = 100
        self.assertIsNotNone(r.reserve('u')[1], 'pending work must not expire')
        r.finish(token, False)
        token, rule, _ = r.reserve('u')
        self.assertIsNone(rule)
        r.finish(token, True)
        self.assertIsNotNone(r.reserve('u')[1])
        now[0] = 161
        self.assertIsNone(r.reserve('u')[1])

    def test_limit_whitelist_and_multiple_rules(self):
        r = RateLimiter({'enable_rate_limit': True, 'rate_limit_whitelist': ['admin'], 'rate_limit_rules': [{'window_seconds': 60, 'max_count': 2}, {'window_seconds': 3600, 'max_count': 1}]})
        self.assertEqual(r.reserve('admin'), (None, None, 0))
        r.reserve('u')
        self.assertEqual(r.reserve('u')[1]['window_seconds'], 3600)

    def test_prompt_command_delete_persists(self):
        config = Config(prompt=[{'trigger': 'x', 'prompt': '{{user_text}}'}])
        p = PromptManager(config['prompt'], self.path, config)
        p.delete('x')
        self.assertEqual(PromptManager(config['prompt'], self.path, config).list_prompts(), [])

    def test_prompt_webui_delete_persists(self):
        PromptManager([{'trigger': 'x', 'prompt': 'a'}], self.path)
        self.assertEqual(PromptManager([], self.path).list_prompts(), [])

    def test_prompt_legacy_migration_preserves_commands(self):
        (self.path / 'prompts.json').write_text('[{"trigger":"local","prompt":"test"}]', encoding='utf-8')
        p = PromptManager(['nd {{user_text}}'], self.path)
        self.assertEqual(set(p.list_prompts()), {'local', 'nd'})

    def test_whitelist_command_revoke_persists(self):
        config = Config(user_whitelist=['123'], group_whitelist=[])
        g = WhitelistGuard(True, [], ['123'], self.path, config)
        g.delete('用户', '123')
        self.assertFalse(WhitelistGuard(True, [], config['user_whitelist'], self.path, config).check('123'))

    def test_whitelist_panel_revoke_persists(self):
        WhitelistGuard(True, [], ['123'], self.path)
        self.assertFalse(WhitelistGuard(True, [], [], self.path).check('123'))

    def image_body(self):
        buf = io.BytesIO()
        PILImage.new('RGB', (8, 8), 'red').save(buf, 'PNG')
        return {'config_key': 'reference_image', 'filename': 'x.png', 'data': base64.b64encode(buf.getvalue()).decode()}

    def test_upload_valid_image(self):
        result = store_upload(self.path, self.image_body())
        self.assertTrue(Path(result['absolute_path']).is_relative_to(self.path / 'files'))
        self.assertTrue(Path(result['absolute_path']).exists())

    def test_upload_blocks_paths(self):
        for key in ['../escaped', '/tmp', 'C:/other', '..\\other', 'unknown']:
            body = self.image_body()
            body['config_key'] = key
            with self.assertRaises(ValueError): store_upload(self.path, body)

    def test_upload_rejects_nonimage(self):
        body = self.image_body()
        body['data'] = base64.b64encode(b'not an image').decode()
        with self.assertRaises(ValueError): store_upload(self.path, body)

    def test_default_config_and_validation(self):
        schema = json.loads((ROOT / '_conf_schema.json').read_text(encoding='utf-8'))
        config = {k: deepcopy(v.get('default')) for k, v in schema.items()}
        validate_config(config, schema)
        for invalid in [0, -1, 'oops', None]:
            with self.assertRaises(ValueError): validate_config({'timeout': invalid}, schema)

    def test_page_waits_for_bridge_and_loads_sdk(self):
        script = (ROOT / 'pages/neko-draw/assets/app.js').read_text(encoding='utf-8')
        template = (ROOT / 'dashboard/templates/index.html.j2').read_text(encoding='utf-8')
        styles = (ROOT / 'pages/neko-draw/assets/style.css').read_text(encoding='utf-8')
        self.assertIn('await connectBridge()', script)
        self.assertNotIn('/api/plugin/page/bridge-sdk.js', template)
        self.assertIn('页面桥接初始化超时', script)
        self.assertIn('id="page-apng"', template)
        self.assertIn('id="page-providers"', template)
        self.assertNotIn('id="page-appearance"', template)
        self.assertNotIn('astrbot-plugin-neko-draw-theme', template)
        self.assertIn('integratedDefaultSelectField', script)
        self.assertIn('imageProvidersField', script)
        self.assertIn('renderProvidersPage', script)
        self.assertIn('custom_model: false', script)
        self.assertIn('drawRows(filter)', script)
        self.assertIn('.discovered-model[hidden]{display:none}', styles)
        self.assertIn('id="global-save"', template)
        self.assertIn('保存并重载', template)
        self.assertIn('floating-config-actions', template)
        self.assertNotIn('config-actions-toggle', template)
        self.assertIn('aria-label="重新加载配置"', template)
        self.assertIn('aria-label="保存并重载"', template)
        self.assertIn('正在读取原始生成图', script)
        self.assertIn('下载原图', script)
        self.assertIn('style.css?v={{ version }}', template)
        self.assertIn('app.js?v={{ version }}', template)
        self.assertNotIn('sidebar-config-actions', template)
        self.assertNotIn('界面风格仅在本次打开期间生效', script)
        root_theme = styles.split('html[data-theme="atelier"]', 1)[0]
        self.assertIn('--accent: #4da47c', root_theme)
        self.assertIn('html[data-theme="atelier"]', styles)
        self.assertNotIn('localStorage', template)
        self.assertNotIn('if (key === "webui_theme") applyTheme(next)', script)
        self.assertIn('providers/test', script)
        self.assertIn('providers/models', script)
        self.assertIn('models/test', script)

    def test_rate_limiter_reconfigure_preserves_usage(self):
        limiter = RateLimiter({'enable_rate_limit': True, 'rate_limit_rules': [
            {'window_seconds': 60, 'max_count': 2},
        ]})
        token, _, _ = limiter.reserve('user')
        limiter.finish(token, success=True)
        completed = limiter.completed['user'][:]
        limiter.reconfigure({'enable_rate_limit': True, 'rate_limit_rules': [
            {'window_seconds': 60, 'max_count': 1},
        ]})
        self.assertEqual(limiter.completed['user'], completed)
        token, rule, _ = limiter.reserve('user')
        self.assertIsNone(token)
        self.assertEqual(rule['max_count'], 1)

    def test_config_rejects_duplicate_model_names_and_prompt_triggers(self):
        schema = json.loads((ROOT / '_conf_schema.json').read_text(encoding='utf-8'))
        providers = [{'__template_key': 'provider_item', 'name': 'OpenAI', 'type': 'OpenAI', 'api_key': '', 'base_url': 'https://api.openai.com/v1'}]
        with self.assertRaisesRegex(ValueError, '模型模板名称必须唯一'):
            validate_config({'model_providers': providers, 'model_templates': [
                {'name': 'same', 'model': 'first', 'provider': 'OpenAI'},
                {'name': 'same', 'model': 'second', 'provider': 'OpenAI'},
            ]}, schema)
        with self.assertRaisesRegex(ValueError, '预设提示词触发词必须唯一'):
            validate_config({'prompt': [
                {'trigger': 'draw', 'prompt': 'first'},
                {'trigger': 'draw', 'prompt': 'second'},
            ]}, schema)
        normalized = validate_config({'model_providers': providers, 'model_templates': [{
            '__template_key': 'model_template',
            'name': 'only_one',
            'model': 'example/model',
            'provider': 'OpenAI',
        }]}, schema)
        self.assertEqual(normalized['model_templates'][0]['provider'], 'OpenAI')

    def test_config_rejects_invalid_model_providers(self):
        schema = json.loads((ROOT / '_conf_schema.json').read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ValueError, '模型提供商名称必须唯一'):
            validate_config({'model_providers': [
                {'name': 'same', 'type': 'OpenAI'},
                {'name': 'same', 'type': 'WaveSpeed'},
            ]}, schema)
        with self.assertRaisesRegex(ValueError, '类型无效'):
            validate_config({'model_providers': [{'name': 'bad', 'type': 'unknown'}]}, schema)
        with self.assertRaisesRegex(ValueError, '必须选择 AstrBot 模型提供商'):
            validate_config({'model_providers': [{'name': 'host', 'type': 'AstrBot'}]}, schema)
        with self.assertRaisesRegex(ValueError, '不存在'):
            validate_config({
                'model_providers': [{'name': 'OpenAI', 'type': 'OpenAI'}],
                'model_templates': [{'name': 'model', 'model': 'x', 'provider': 'missing'}],
            }, schema)

    def test_history_pagination_and_cleanup(self):
        store = load('core.history').HistoryStore(self.path)
        record = store.add(user_id='123', status='success')
        self.assertEqual(store.list(user_id='123')['total'], 1)
        self.assertEqual(store.get(record)['user_id'], '123')
        self.assertTrue(store.delete(record))
        self.assertEqual(store.stats()['total'], 0)

    def test_apng_history_deletes_managed_file(self):
        save_dir = self.path / 'save_images'; save_dir.mkdir()
        output = save_dir / 'apng_maker_test.png'; output.write_bytes(b'png')
        store = load('core.apng_history').ApngHistoryStore(self.path)
        record_id = store.add(user_id='1', group_id='2', frame_count=3, duration_ms=4000, loop=0, file_path=str(output))
        self.assertEqual(store.list()['total'], 1)
        self.assertTrue(store.delete(record_id))
        self.assertFalse(output.exists())

    def test_history_stores_edit_source_paths(self):
        store = load('core.history').HistoryStore(self.path)
        record_id = store.add(status='success', source_image_paths=['source.png'])
        self.assertEqual(store.get(record_id)['source_image_paths'], ['source.png'])


def install_astrbot_stubs():
    # Only the host boundary is mocked; plugin business code runs unchanged.
    for name in ['astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.message_components', 'astrbot.api.star', 'astrbot.core']:
        sys.modules[name] = types.ModuleType(name)
    import logging
    sys.modules['astrbot.api'].logger = logging.getLogger('tests')
    class Filters:
        PermissionType = types.SimpleNamespace(ADMIN='admin')
        EventMessageType = types.SimpleNamespace(ALL='all')
        class Group:
            def __init__(self, func): self.func = func
            def __get__(self, obj, owner): return self.func.__get__(obj, owner)
            def command(self, *args, **kwargs): return lambda func: func
        def command_group(self, *args, **kwargs):
            def decorate(func):
                if inspect.signature(func).parameters:
                    raise TypeError('command_group placeholder must not declare parameters')
                return self.Group(func)
            return decorate
        def __getattr__(self, name):
            return lambda *args, **kwargs: lambda func: func
    sys.modules['astrbot.api.event'].filter = Filters()
    sys.modules['astrbot.api.event'].AstrMessageEvent = object
    class Component:
        def __init__(self, value=''):
            self.value = value
            self.file = value
        @staticmethod
        def fromFileSystem(path): return Component(path)
    for name in ['At', 'File', 'Image', 'Plain', 'Reply']:
        setattr(sys.modules['astrbot.api.message_components'], name, type(name, (Component,), {}))
    class Star:
        def __init__(self, context): pass
    sys.modules['astrbot.api.star'].Star = Star
    sys.modules['astrbot.api.star'].Context = object
    sys.modules['astrbot.api.star'].StarTools = types.SimpleNamespace()
    sys.modules['astrbot.core'].AstrBotConfig = Config


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        install_astrbot_stubs()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        sys.modules['astrbot.api.star'].StarTools.get_data_dir = lambda *args: self.path
        self.main = load('main')
        self.main.StarTools.get_data_dir = lambda *args: self.path
        schema = json.loads((ROOT / '_conf_schema.json').read_text(encoding='utf-8'))
        self.config = Config({k: deepcopy(v.get('default')) for k, v in schema.items()})
        self.config['enable_rate_limit'] = True
        self.config['rate_limit_rules'] = [{'window_seconds': 60, 'max_count': 1}]
        self.context = types.SimpleNamespace(register_web_api=lambda *args: None)
        self.plugin = self.main.NekoDrawPlugin(self.context, self.config)
        self.event = types.SimpleNamespace(message_str='nd cat', get_messages=lambda: [], get_sender_id=lambda: '123', get_group_id=lambda: '', plain_result=lambda text: text, chain_result=lambda chain: chain, send=AsyncMock())

    async def test_ordinary_message_silent_when_limited(self):
        self.plugin.rate_limiter.reserve('123')
        self.event.message_str = '你好'
        self.plugin.handler.handle = AsyncMock()
        self.assertEqual([x async for x in self.plugin.on_message(self.event)], [])
        self.plugin.handler.handle.assert_not_called()

    async def test_old_default_drawing_message_is_migrated(self):
        config = Config(self.config)
        config['drawing_message'] = '🐱 猫娘正在画图，请稍候...'
        plugin = self.main.NekoDrawPlugin(self.context, config)
        self.assertEqual(plugin.conf['drawing_message'], '小猫正在搓屏幕中')

    async def test_generation_exception_releases_quota(self):
        self.plugin.handler.handle = AsyncMock(side_effect=RuntimeError('test'))
        with self.assertRaises(RuntimeError):
            [x async for x in self.plugin.on_message(self.event)]
        self.assertFalse(self.plugin.rate_limiter.pending)

    async def test_cancellation_releases_quota(self):
        self.plugin.handler.handle = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            [x async for x in self.plugin.on_message(self.event)]
        self.assertFalse(self.plugin.rate_limiter.pending)

    async def test_send_combinations_preserve_fallback(self):
        result = load('core.handler').HandlerResult(images=['result.png'])
        for forward in [False, True]:
            for summary in [False, True]:
                self.plugin.rate_limiter.completed.clear()
                self.config.update(enable_forward_message=forward, enable_image_summary=summary, enable_apng_wrap=False)
                self.plugin.handler.handle = AsyncMock(return_value=result)
                self.plugin.output._send_forward_message = AsyncMock(return_value=False)
                self.plugin.output._send_image_with_summary = AsyncMock(return_value=False)
                outputs = [x async for x in self.plugin.on_message(self.event)]
                self.assertEqual(len(outputs), 1)
                self.assertEqual(outputs[0][0].value, 'result.png')
                self.assertEqual(self.plugin.output._send_forward_message.await_count, int(forward))
                self.assertEqual(self.plugin.output._send_image_with_summary.await_count, int(summary))

    async def test_apng_two_frames(self):
        self.config['enable_apng_wrap'] = True
        source = self.path / 'test.png'
        PILImage.new('RGB', (32, 32), 'blue').save(source)
        output = self.plugin.output._wrap_apng([str(source)])[0]
        with PILImage.open(output) as img:
            self.assertEqual(img.n_frames, 2)

    async def test_generated_apng_cleanup_keeps_original(self):
        self.config['enable_apng_wrap'] = True
        source = self.path / 'generated-original.png'
        PILImage.new('RGB', (32, 32), 'blue').save(source)
        wrapped = self.plugin.output._wrap_apng([str(source)])[0]
        self.assertNotEqual(Path(wrapped), source)
        self.assertTrue(Path(wrapped).exists())
        self.plugin.output.cleanup_transient_apngs([wrapped], [str(source)])
        self.assertFalse(Path(wrapped).exists())
        self.assertTrue(source.exists())

    async def test_apng_maker_preserves_order_and_duration(self):
        paths = []
        for index, color in enumerate(('red', 'green', 'blue', 'yellow', 'black')):
            path = self.path / f'frame-{index}.png'
            PILImage.new('RGB', (24 + index, 20), color).save(path)
            paths.append(str(path))
        output = self.plugin.output.make_apng(paths, 4000, 0)
        with PILImage.open(output) as img:
            self.assertEqual(img.n_frames, 5)
            self.assertEqual(img.info['duration'], 4000.0)
            self.assertEqual(img.info['loop'], 0)

    async def test_apng_command_options(self):
        self.assertEqual(self.plugin._parse_apng_options('apng'), (5000, 0, 100))
        self.assertEqual(self.plugin._parse_apng_options('apng 4'), (4000, 0, 100))
        self.config['apng_default_interval_seconds'] = 2.5
        self.assertEqual(self.plugin._parse_apng_options('/apng'), (2500, 0, 100))
        self.config['apng_loop'] = 2
        self.assertEqual(self.plugin._parse_apng_options('apng 4'), (4000, 2, 100))
        self.assertEqual(self.plugin._parse_apng_options('/apng 0.5 3'), (500, 3, 100))
        self.assertEqual(self.plugin._parse_apng_options('apng 4 --fd 1.5'), (4000, 2, 1500))
        with self.assertRaisesRegex(ValueError, '0.1～60'):
            self.plugin._parse_apng_options('apng 0')

    async def test_drawing_and_apng_first_frames_are_independent(self):
        drawing = self.path / 'drawing-first.png'
        apng = self.path / 'apng-first.png'
        PILImage.new('RGB', (12, 12), 'orange').save(drawing)
        PILImage.new('RGB', (12, 12), 'purple').save(apng)
        self.config['drawing_first_frame_path'] = [str(drawing)]
        self.config['apng_first_frame_path'] = [str(apng)]
        self.assertEqual(self.plugin.output._resolve_apng_first_frame('drawing'), drawing)
        self.assertEqual(self.plugin.output._resolve_apng_first_frame('apng'), apng)

    async def test_apng_delivery_does_not_use_drawing_switches(self):
        self.config.update(enable_forward_message=True, enable_image_summary=True)
        self.plugin.output._send_forward_message = AsyncMock(return_value=False)
        self.plugin.output._send_image_with_summary = AsyncMock(return_value=False)
        await self.plugin._send_apng_file(self.event, 'result.png')
        self.plugin.output._send_forward_message.assert_not_called()
        self.plugin.output._send_image_with_summary.assert_not_called()
        self.event.send.assert_awaited_once()

        self.event.send.reset_mock()
        self.config['apng_enable_forward_message'] = True
        self.plugin.output._send_forward_message = AsyncMock(return_value=True)
        await self.plugin._send_apng_file(self.event, 'result.png')
        self.plugin.output._send_forward_message.assert_awaited_once_with(
            self.event, ['result.png'], profile='apng'
        )
        self.event.send.assert_not_awaited()

    async def test_plain_apng_message_builds_animation(self):
        Image = self.main.Image
        paths = []
        for index, color in enumerate(('red', 'blue', 'green')):
            path = self.path / f'message-{index}.png'
            PILImage.new('RGB', (20, 20), color).save(path)
            paths.append(path)
        self.event.message_str = 'apng 4'
        self.event.get_messages = lambda: [Image(str(path)) for path in paths]
        outputs = [x async for x in self.plugin.on_message(self.event)]
        self.assertEqual(outputs, [])
        sent = self.event.send.await_args.args[0]
        with PILImage.open(sent[0].value) as img:
            self.assertEqual(img.n_frames, 3)
            self.assertEqual(img.info['duration'], 100.0)
            img.seek(1)
            self.assertEqual(img.info['duration'], 4000.0)
        self.assertIn('猫娘正在制作 APNG', self.event.send.await_args_list[0].args[0])

    async def test_apng_progress_message_can_be_disabled(self):
        self.config['apng_enable_drawing_message'] = False
        paths = []
        for index, color in enumerate(('red', 'blue')):
            path = self.path / f'quiet-{index}.png'
            PILImage.new('RGB', (20, 20), color).save(path)
            paths.append(path)
        self.event.message_str = 'apng 1'
        self.event.get_messages = lambda: [self.main.Image(str(path)) for path in paths]
        [x async for x in self.plugin.on_message(self.event)]
        self.event.send.assert_awaited_once()

    async def test_single_image_uses_configured_first_frame(self):
        self.config['apng_maker_min_frames'] = 1
        source = self.path / 'single.png'; PILImage.new('RGB', (20, 20), 'red').save(source)
        self.event.message_str = 'apng 4'
        self.event.get_messages = lambda: [self.main.Image(str(source))]
        [x async for x in self.plugin.on_message(self.event)]
        sent = self.event.send.await_args.args[0]
        with PILImage.open(sent[0].value) as img:
            self.assertEqual(img.n_frames, 2)
            self.assertEqual(img.info['duration'], 100.0)
            img.seek(1); self.assertEqual(img.info['duration'], 4000.0)

    async def test_cleanup_after_send_removes_apng_and_skips_history(self):
        self.config['apng_cleanup_after_send'] = True
        paths = []
        for index, color in enumerate(('red', 'blue')):
            path = self.path / f'cleanup-{index}.png'; PILImage.new('RGB', (20, 20), color).save(path); paths.append(path)
        self.event.message_str = 'apng 1'
        self.event.get_messages = lambda: [self.main.Image(str(path)) for path in paths]
        [x async for x in self.plugin.on_message(self.event)]
        sent_path = Path(self.event.send.await_args.args[0][0].value)
        self.assertFalse(sent_path.exists())
        self.assertEqual(self.plugin.apng_history_store.list()['total'], 0)

    async def test_forward_record_images_are_expanded(self):
        class Forward:
            def __init__(self, value): self.id = value
        reply = self.main.Reply(); reply.chain = [Forward('forward-1')]
        self.event.get_messages = lambda: [reply]
        self.event.message_obj = types.SimpleNamespace(raw_message={})
        call = AsyncMock(return_value={'data': {'message': [
            {'type': 'node', 'data': {'content': [
                {'type': 'image', 'data': {'url': 'https://example.test/1.png'}},
                {'type': 'image', 'data': {'file': 'https://example.test/2.png'}},
            ]}},
        ]}})
        self.event.bot = types.SimpleNamespace(api=types.SimpleNamespace(call_action=call))
        refs = await self.plugin._extract_all_image_urls(self.event)
        self.assertEqual(refs, ['https://example.test/1.png', 'https://example.test/2.png'])
        call.assert_awaited_once_with('get_forward_msg', id='forward-1')

    async def test_save_and_reset_first_frame(self):
        source = self.path / 'first.png'
        PILImage.new('RGB', (18, 16), 'purple').save(source)
        target = self.plugin.output.save_first_frame(str(source))
        self.config['apng_first_frame_path'] = [str(target)]
        self.assertEqual(self.plugin.output._resolve_apng_first_frame(), target)
        outputs = [x async for x in self.plugin.reset_first_frame_command(self.event)]
        self.assertTrue(outputs)
        self.assertEqual(self.config['apng_first_frame_path'], [])

    async def test_reference_data_uri(self):
        uri = 'data:image/png;base64,dGVzdA=='
        self.assertEqual(await self.plugin.downloader.download_to_data_uri(uri), uri)

    async def test_migrated_command_only_panel_delete(self):
        (self.path / 'prompts.json').write_text('[{"trigger":"legacy","prompt":"x"}]', encoding='utf-8')
        plugin = self.main.NekoDrawPlugin(self.context, self.config)
        self.assertIn('legacy', plugin.prompt_manager.list_prompts())
        self.config['prompt'] = [p for p in self.config['prompt'] if p['trigger'] != 'legacy']
        plugin = self.main.NekoDrawPlugin(self.context, self.config)
        self.assertNotIn('legacy', plugin.prompt_manager.list_prompts())

    async def test_webui_routes_use_full_plugin_identifier(self):
        routes = []
        context = types.SimpleNamespace(
            register_web_api=lambda path, *_args: routes.append(path)
        )
        self.main.NekoDrawPlugin(context, self.config)
        self.assertTrue(routes)
        self.assertTrue(all(path.startswith('/astrbot_plugin_neko_draw/') for path in routes))

    async def test_webui_config_validation_and_save(self):
        web = load('webui')
        previous = web.request
        try:
            web.request = types.SimpleNamespace(json=AsyncMock(return_value={'config': {'timeout': 0}}))
            result = await self.plugin.webui_bridge.api_save_config()
            self.assertEqual(result['status_code'], 400)
            self.assertNotEqual(self.config['timeout'], 0)
            web.request = types.SimpleNamespace(json=AsyncMock(return_value={'config': {'timeout': 123}}))
            result = await self.plugin.webui_bridge.api_save_config()
            self.assertEqual(result['status_code'], 200)
            self.assertEqual(self.config['timeout'], 123)
        finally:
            web.request = previous

    async def test_webui_failed_save_rolls_back(self):
        web = load('webui')
        previous = web.request
        old = deepcopy(dict(self.config))
        self.config.save_config = lambda: (_ for _ in ()).throw(OSError('disk full'))
        try:
            web.request = types.SimpleNamespace(json=AsyncMock(return_value={'config': {'timeout': 123}}))
            result = await self.plugin.webui_bridge.api_save_config()
            self.assertEqual(result['status_code'], 500)
            self.assertEqual(dict(self.config), old)
        finally:
            web.request = previous

    async def test_webui_runtime_reload_failure_rolls_back_persisted_config(self):
        web = load('webui')
        previous_request = web.request
        previous_callback = self.plugin.webui_bridge.on_config_saved
        old = deepcopy(dict(self.config))
        self.config.save_config = MagicMock()
        self.plugin.webui_bridge.on_config_saved = AsyncMock(
            side_effect=RuntimeError('runtime build failed')
        )
        try:
            web.request = types.SimpleNamespace(
                json=AsyncMock(return_value={'config': {'timeout': 123}})
            )
            result = await self.plugin.webui_bridge.api_save_config()
            self.assertEqual(result['status_code'], 500)
            self.assertEqual(dict(self.config), old)
            self.assertEqual(self.config.save_config.call_count, 2)
        finally:
            self.plugin.webui_bridge.on_config_saved = previous_callback
            web.request = previous_request

    async def test_successful_forward_does_not_duplicate(self):
        self.config.update(enable_forward_message=True, enable_image_summary=True, enable_apng_wrap=False)
        self.plugin.handler.handle = AsyncMock(return_value=load('core.handler').HandlerResult(images=['result.png']))
        self.plugin.output._send_forward_message = AsyncMock(return_value=True)
        self.plugin.output._send_image_with_summary = AsyncMock(return_value=True)
        self.assertEqual([x async for x in self.plugin.on_message(self.event)], [])
        self.plugin.output._send_image_with_summary.assert_not_called()


if __name__ == '__main__':
    unittest.main()
