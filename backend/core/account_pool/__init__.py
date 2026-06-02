"""
账号池 - 完整实现（对齐 ds2api）

将 pool_core.py 和 pool_acquire.py 合并为完整的 AccountPool
"""
import asyncio
import logging
import time
from typing import Optional

from backend.core.account_pool.pool_core import Account, AccountPool as CorePool
from backend.core.account_pool.pool_acquire import AccountAcquireMixin

log = logging.getLogger("web2api.accounts")


class AccountPool(AccountAcquireMixin, CorePool):
    """
    完整的账号池实现

    对齐 ds2api 的 4 层并发控制：
    1. max_inflight_per_account: 每账号最大并发
    2. recommended_concurrency: 推荐并发值
    3. max_queue_size: 等待队列上限
    4. global_max_inflight: 全局最大并发
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 匿名 token 缓存
        self._anonymous_token: Optional[str] = None
        self._anonymous_token_fetched_at: float = 0.0
        self._anonymous_token_ttl: float = 300.0  # 5 分钟缓存
        self._anonymous_fetch_lock = asyncio.Lock()

    async def get_anonymous_token(self) -> Optional[str]:
        """获取匿名 token（带缓存）"""
        now = time.time()

        # 如果缓存有效，直接返回
        if self._anonymous_token and (now - self._anonymous_token_fetched_at) < self._anonymous_token_ttl:
            return self._anonymous_token

        # 使用锁防止并发获取
        async with self._anonymous_fetch_lock:
            # 双重检查
            if self._anonymous_token and (now - self._anonymous_token_fetched_at) < self._anonymous_token_ttl:
                return self._anonymous_token

            # 获取新的匿名 token
            log.info("[Anonymous] 获取新的匿名 token...")
            try:
                from backend.services.auth_resolver import get_anonymous_token
                token = await get_anonymous_token()
                if token:
                    self._anonymous_token = token
                    self._anonymous_token_fetched_at = now
                    log.info(f"[Anonymous] ✓ 匿名 token 已缓存: {token[:20]}...")
                    return token
            except Exception as e:
                log.error(f"[Anonymous] 获取匿名 token 异常: {e}")

            log.warning("[Anonymous] ✗ 获取匿名 token 失败")
            return None

    async def acquire_anonymous(self) -> Optional[Account]:
        """获取匿名账号（用于无账号时的匿名访问）"""
        token = await self.get_anonymous_token()
        if not token:
            return None

        # 创建一个临时的匿名账号对象
        anonymous_acc = Account(
            email="anonymous@qwen",
            password="",
            token=token,
            cookies="",
            username="anonymous",
            activation_pending=False,
        )
        anonymous_acc.valid = True
        anonymous_acc.inflight = 0
        anonymous_acc.last_used = time.time()
        anonymous_acc.last_request_started = time.time()

        return anonymous_acc


__all__ = ["Account", "AccountPool"]
