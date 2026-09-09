"""Construct provider clients from the unchanged public configuration schema."""
from .wavespeed import WavespeedClient
from .runninghub import RunningHubClient
from .openapi import OpenAPIClient


def create_providers(config):
    shared = {
        'proxy': config.get('proxy') or None,
        'timeout': float(config.get('timeout', 300)),
        'max_concurrency': int(config.get('max_concurrency', 2)),
    }
    polling = {'poll_interval': float(config.get('poll_interval', 2.0))}
    return {
        'wavespeed': WavespeedClient(
            api_key=config.get('api_key', ''),
            base_url=config.get('base_url', 'https://api.wavespeed.ai/api/v3'),
            max_429_retries=int(config.get('max_429_retries', 5)),
            retry_429_delay=float(config.get('retry_429_delay', 2.0)),
            **shared, **polling,
        ),
        'runninghub': RunningHubClient(
            api_key=config.get('runninghub_api_key', ''),
            base_url=config.get('runninghub_base_url', 'https://www.runninghub.cn/openapi/v2'),
            **shared, **polling,
        ),
        'openapi': OpenAPIClient(
            api_key=config.get('openapi_api_key', ''),
            base_url=config.get('openapi_base_url', 'https://api.openai.com/v1'),
            **shared,
        ),
    }
