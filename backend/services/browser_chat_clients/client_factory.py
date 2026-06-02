"""
客户端工厂

提供统一的客户端创建接口，支持根据站点名称创建对应的客户端实例。
"""

import logging
from typing import Dict, Type, Optional, List

from .base_client import BaseBrowserChatClient, ClientConfig
from .qwen_ai_client import QwenAIClient
from .qianwen_client import QianwenClient
from .doubao_client import DoubaoClient

log = logging.getLogger("web2api.browser_chat.factory")

# 站点名称到客户端类的映射
_CLIENT_REGISTRY: Dict[str, Type[BaseBrowserChatClient]] = {
    "qwen.ai": QwenAIClient,
    "chat.qwen.ai": QwenAIClient,
    "qianwen": QianwenClient,
    "qianwen.com": QianwenClient,
    "www.qianwen.com": QianwenClient,
    "doubao": DoubaoClient,
    "doubao.com": DoubaoClient,
    "www.doubao.com": DoubaoClient,
}

# 默认站点
DEFAULT_SITE = "qwen.ai"


class ClientFactory:
    """
    客户端工厂类

    使用示例::

        # 使用工厂方法
        client = ClientFactory.create("qwen.ai")
        async with client:
            response = await client.chat("你好")

        # 使用便捷函数
        client = get_client("doubao.com")

        # 获取所有可用站点
        sites = get_available_sites()
    """

    @staticmethod
    def create(
        site: str = DEFAULT_SITE,
        config: Optional[ClientConfig] = None,
        **kwargs
    ) -> BaseBrowserChatClient:
        """
        创建客户端实例

        Args:
            site: 站点名称或 URL
            config: 客户端配置
            **kwargs: 传递给客户端构造函数的额外参数

        Returns:
            客户端实例

        Raises:
            ValueError: 如果站点不支持
        """
        # 标准化站点名称
        site_lower = site.lower().strip()

        # 尝试直接匹配
        client_class = _CLIENT_REGISTRY.get(site_lower)

        # 尝试模糊匹配
        if not client_class:
            for key, cls in _CLIENT_REGISTRY.items():
                if key in site_lower or site_lower in key:
                    client_class = cls
                    break

        if not client_class:
            available = list(set(_CLIENT_REGISTRY.values()))
            available_names = [cls.__name__ for cls in available]
            raise ValueError(
                f"不支持的站点: {site}。"
                f"可用的客户端: {', '.join(available_names)}"
            )

        # 创建实例
        if config:
            return client_class(config=config, **kwargs)
        else:
            return client_class(**kwargs)

    @staticmethod
    def register(site: str, client_class: Type[BaseBrowserChatClient]) -> None:
        """
        注册新的客户端类

        Args:
            site: 站点名称
            client_class: 客户端类
        """
        _CLIENT_REGISTRY[site.lower()] = client_class
        log.info(f"已注册客户端: {site} -> {client_class.__name__}")

    @staticmethod
    def get_available_sites() -> List[str]:
        """
        获取所有可用的站点名称

        Returns:
            站点名称列表
        """
        return list(set(_CLIENT_REGISTRY.keys()))

    @staticmethod
    def get_client_classes() -> Dict[str, Type[BaseBrowserChatClient]]:
        """
        获取所有注册的客户端类

        Returns:
            站点名称到客户端类的映射
        """
        return _CLIENT_REGISTRY.copy()


def get_client(site: str = DEFAULT_SITE, **kwargs) -> BaseBrowserChatClient:
    """
    便捷函数：创建客户端实例

    Args:
        site: 站点名称或 URL
        **kwargs: 传递给客户端构造函数的额外参数

    Returns:
        客户端实例
    """
    return ClientFactory.create(site, **kwargs)


def get_available_sites() -> List[str]:
    """
    便捷函数：获取所有可用的站点名称

    Returns:
        站点名称列表
    """
    return ClientFactory.get_available_sites()
