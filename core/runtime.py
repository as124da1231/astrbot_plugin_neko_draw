"""Atomic construction and ownership of config-driven runtime services."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .downloader import Downloader
from .handler import DrawingHandler
from .output import OutputService
from .prompt_manager import PromptManager
from .providers import create_providers, ensure_provider_config
from .rate_limit import RateLimiter
from .templates import TemplateManager
from .whitelist import WhitelistGuard


@dataclass
class RuntimeServices:
    """Services replaced together after a configuration save."""

    providers: dict
    templates: TemplateManager
    prompt_manager: PromptManager
    whitelist_guard: WhitelistGuard
    downloader: Downloader
    handler: DrawingHandler
    output: OutputService
    rate_limiter: RateLimiter

    @classmethod
    def build(
        cls,
        config: Any,
        *,
        context: Any,
        data_dir: Path,
        save_dir: Path,
        plugin_dir: Path,
        previous: Optional["RuntimeServices"] = None,
    ) -> "RuntimeServices":
        """Build a complete replacement before exposing any of its parts."""
        ensure_provider_config(config)
        providers = create_providers(config, context=context)
        templates = TemplateManager(config.get("model_templates", []))
        prompt_manager = PromptManager(config.get("prompt", []), data_dir, config=config)
        whitelist_guard = WhitelistGuard(
            enabled=bool(config.get("whitelist_enabled", False)),
            group_whitelist=config.get("group_whitelist", []),
            user_whitelist=config.get("user_whitelist", []),
            data_dir=data_dir,
            config=config,
        )
        downloader = Downloader(save_dir, proxy=config.get("proxy") or None)
        handler = DrawingHandler(
            templates=templates,
            default_text_model=config.get("default_text_model", ""),
            default_edit_model=config.get("default_edit_model", ""),
            prompt_manager=prompt_manager,
            whitelist_guard=whitelist_guard,
            downloader=downloader,
            providers=providers,
        )
        output = OutputService(config, data_dir, save_dir, plugin_dir)
        rate_limiter = previous.rate_limiter if previous else RateLimiter(config)
        rate_limiter.reconfigure(config)
        return cls(
            providers=providers,
            templates=templates,
            prompt_manager=prompt_manager,
            whitelist_guard=whitelist_guard,
            downloader=downloader,
            handler=handler,
            output=output,
            rate_limiter=rate_limiter,
        )

    async def close(self) -> None:
        """Close owned network clients once, tolerating provider aliases."""
        seen: set[int] = set()
        for client in list(self.providers.values()) + [self.downloader]:
            marker = id(client)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                await client.close()
            except Exception:
                pass
