"""Per-user quotas with atomic in-flight reservations (single event loop)."""
import time
import uuid


# 预留槽位最多占用这么久；超时视为「事件被取消 / 卡死」，自动释放，避免用户被永久限流
PENDING_TTL_SECONDS = 600


class RateLimiter:
    def __init__(self, config, clock=time.monotonic):
        self.config = config
        self.clock = clock
        self.completed = {}
        self.pending = {}
        self._pending_at = {}

    def reconfigure(self, config):
        """Apply new rules without losing completed or in-flight usage."""
        self.config = config

    def rules(self):
        rules = []
        for item in self.config.get('rate_limit_rules', []) or []:
            try:
                window, count = int(item['window_seconds']), int(item['max_count'])
                if window > 0 and count > 0:
                    rules.append({'window_seconds': window, 'max_count': count})
            except (KeyError, TypeError, ValueError):
                continue
        return rules

    def reserve(self, user_id):
        uid = str(user_id)
        rules = self.rules()
        if (not self.config.get('enable_rate_limit', False) or not rules
                or uid in {str(x) for x in self.config.get('rate_limit_whitelist', []) or []}):
            return None, None, 0
        now = self.clock()
        longest = max(r['window_seconds'] for r in rules)
        for key, values in list(self.completed.items()):
            fresh = [t for t in values if now - t < longest]
            if fresh:
                self.completed[key] = fresh
            else:
                self.completed.pop(key, None)
        self._expire_pending(now)
        pending = sum(owner == uid for owner in self.pending.values())
        for rule in rules:
            values = sorted(t for t in self.completed.get(uid, []) if now - t < rule['window_seconds'])
            if len(values) + pending >= rule['max_count']:
                retry = max(1, int(rule['window_seconds'] - (now - values[0])) + 1) if values else 1
                return None, rule, retry
        token = uuid.uuid4().hex
        self.pending[token] = uid
        self._pending_at[token] = now
        return token, None, 0

    def _expire_pending(self, now=None):
        """回收超时未结束的预留槽位（事件被平台取消、拆解卡死等）。"""
        now = self.clock() if now is None else now
        stale = [token for token, at in self._pending_at.items()
                 if now - at > PENDING_TTL_SECONDS]
        for token in stale:
            self._pending_at.pop(token, None)
            self.pending.pop(token, None)
        if stale:
            return len(stale)
        return 0

    def release(self, token):
        """主动释放预留槽位（事件被取消时用），不计入成功次数。"""
        self.pending.pop(token, None)
        self._pending_at.pop(token, None)

    def finish(self, token, success=False):
        uid = self.pending.pop(token, None)
        self._pending_at.pop(token, None)
        if uid is not None and success:
            self.completed.setdefault(uid, []).append(self.clock())

    @staticmethod
    def format_window(seconds):
        if seconds < 60:
            return f'{seconds}秒'
        if seconds < 3600:
            return f'{seconds // 60}分钟'
        if seconds < 86400:
            return f'{seconds // 3600}小时'
        return f'{seconds // 86400}天'
