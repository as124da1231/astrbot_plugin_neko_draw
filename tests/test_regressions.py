"""Offline regression coverage: python -m unittest discover -s tests -v."""
import asyncio
import base64
from copy import deepcopy
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
PKG = ROOT.name
def load(name):
    return importlib.import_module(f'{PKG}.{name}')

templates = load('core.templates')
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

    def test_provider_derivation_follows_template_type(self):
        for key, provider in templates.PROVIDERS.items():
            item = {'name': key, 'model': 'm', '__template_key': key}
            self.assertEqual(templates.TemplateManager([item]).get(key).provider, provider)
        self.assertEqual(templates.derive_provider({'provider': 'openapi', '__template_key': 'seedream_text'}), 'wavespeed')
        self.assertEqual(templates.derive_provider({'provider': 'openapi', '__template_key': 'custom'}), 'openapi')

    def test_structured_payload(self):
        values = {'nested': {'a': [1, None]}, 'arr': [1, 2], 'empty': None, 'flag': True}
        t = templates.ModelTemplate('x', 'm', params=values)
        self.assertEqual(templates.build_payload('cat', {}, t), {**values, 'prompt': 'cat'})

    def test_quoted_preset(self):
        p = parser.parse_prompt_message('猫娘画图 cat', ['猫娘画图 {{user_text}} --negative_prompt "bad quality"'])
        self.assertEqual(p.text, 'cat')
        self.assertEqual(p.params['negative_prompt'], 'bad quality')

    def test_empty_template_no_duplicate_user_text(self):
        self.assertEqual(parser.parse_prompt_message('draw cat', ['draw']).text, 'cat')

    def test_user_param_override(self):
        p = parser.parse_prompt_message('猫娘画图 cat --n 2', ['猫娘画图 {{user_text}} --n 1'])
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
        p = PromptManager(['猫娘画图 {{user_text}}'], self.path)
        self.assertEqual(set(p.list_prompts()), {'local', '猫娘画图'})

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

    def test_config_rejects_duplicate_model_names_and_prompt_triggers(self):
        schema = json.loads((ROOT / '_conf_schema.json').read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ValueError, '模型模板名称必须唯一'):
            validate_config({'model_templates': [
                {'name': 'same', 'model': 'first'},
                {'name': 'same', 'model': 'second'},
            ]}, schema)
        with self.assertRaisesRegex(ValueError, '预设提示词触发词必须唯一'):
            validate_config({'prompt': [
                {'trigger': 'draw', 'prompt': 'first'},
                {'trigger': 'draw', 'prompt': 'second'},
            ]}, schema)
        normalized = validate_config({'model_templates': [{
            '__template_key': 'seedream_text',
            'name': 'only_one',
            'model': 'example/model',
            'provider': 'openapi',
        }]}, schema)
        self.assertEqual(normalized['model_templates'][0]['provider'], 'wavespeed')

    def test_history_pagination_and_cleanup(self):
        store = load('core.history').HistoryStore(self.path)
        record = store.add(user_id='123', status='success')
        self.assertEqual(store.list(user_id='123')['total'], 1)
        self.assertEqual(store.get(record)['user_id'], '123')
        self.assertTrue(store.delete(record))
        self.assertEqual(store.stats()['total'], 0)


def install_astrbot_stubs():
    # Only the host boundary is mocked; plugin business code runs unchanged.
    for name in ['astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.message_components', 'astrbot.api.star', 'astrbot.core']:
        sys.modules[name] = types.ModuleType(name)
    import logging
    sys.modules['astrbot.api'].logger = logging.getLogger('tests')
    class Filters:
        PermissionType = types.SimpleNamespace(ADMIN='admin')
        EventMessageType = types.SimpleNamespace(ALL='all')
        def __getattr__(self, name):
            return lambda *args, **kwargs: lambda func: func
    sys.modules['astrbot.api.event'].filter = Filters()
    sys.modules['astrbot.api.event'].AstrMessageEvent = object
    class Component:
        def __init__(self, value=''): self.value = value
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
        self.event = types.SimpleNamespace(message_str='猫娘画图 cat', get_messages=lambda: [], get_sender_id=lambda: '123', get_group_id=lambda: '', plain_result=lambda text: text, chain_result=lambda chain: chain, send=AsyncMock())

    async def test_ordinary_message_silent_when_limited(self):
        self.plugin.rate_limiter.reserve('123')
        self.event.message_str = '你好'
        self.plugin.handler.handle = AsyncMock()
        self.assertEqual([x async for x in self.plugin.on_message(self.event)], [])
        self.plugin.handler.handle.assert_not_called()

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

    async def test_successful_forward_does_not_duplicate(self):
        self.config.update(enable_forward_message=True, enable_image_summary=True, enable_apng_wrap=False)
        self.plugin.handler.handle = AsyncMock(return_value=load('core.handler').HandlerResult(images=['result.png']))
        self.plugin.output._send_forward_message = AsyncMock(return_value=True)
        self.plugin.output._send_image_with_summary = AsyncMock(return_value=True)
        self.assertEqual([x async for x in self.plugin.on_message(self.event)], [])
        self.plugin.output._send_image_with_summary.assert_not_called()


if __name__ == '__main__':
    unittest.main()
