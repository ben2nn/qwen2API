"""
浏览器聊天客户端抽象基类

定义所有 AI 聊天网站客户端必须实现的接口。
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional, List, Dict, Any

log = logging.getLogger("web2api.browser_chat")


@dataclass
class ChatResponse:
    """聊天响应"""
    content: str
    success: bool = True
    error: str = ""
    images: List[str] = field(default_factory=list)  # 生成的图片 URL
    metadata: Dict[str, Any] = field(default_factory=dict)  # 额外元数据


@dataclass
class ClientConfig:
    """客户端配置"""
    # 浏览器配置
    headless: bool = True
    pool_size: int = 5
    timeout: int = 120  # 等待回复的超时时间（秒）

    # 站点特定配置
    site_url: str = ""
    guest_url: str = ""

    # 调试配置
    debug_screenshots: bool = False
    screenshot_dir: str = ""

    # 额外配置
    extra: Dict[str, Any] = field(default_factory=dict)


class BaseBrowserChatClient(ABC):
    """
    浏览器聊天客户端抽象基类

    所有 AI 聊天网站客户端必须继承此类并实现抽象方法。

    使用示例::

        async with QwenAIClient() as client:
            response = await client.chat("你好")
            print(response.content)

        # 或者手动管理生命周期
        client = QwenAIClient()
        await client.start()
        try:
            response = await client.chat("你好")
            print(response.content)
        finally:
            await client.close()
    """

    def __init__(self, config: Optional[ClientConfig] = None):
        """
        初始化客户端

        Args:
            config: 客户端配置，如果为 None 则使用默认配置
        """
        self._config = config or self.get_default_config()
        self._is_ready = False
        self._browser = None
        self._page = None
        self._tab_pool: List[Any] = []
        self._current_idx = 0
        self._lock = asyncio.Lock()

    # ── 抽象属性 ──

    @property
    @abstractmethod
    def site_name(self) -> str:
        """站点名称，如 'qwen.ai', 'qianwen.com', 'doubao.com'"""
        pass

    @property
    @abstractmethod
    def site_url(self) -> str:
        """站点基础 URL"""
        pass

    # ── 抽象方法：生命周期 ──

    @abstractmethod
    async def start(self, retries: int = 3) -> bool:
        """
        启动浏览器并初始化

        Args:
            retries: 启动重试次数

        Returns:
            是否启动成功
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """关闭浏览器和所有资源"""
        pass

    @abstractmethod
    async def _check_alive(self) -> bool:
        """检查浏览器是否存活"""
        pass

    # ── 抽象方法：页面交互 ──

    @abstractmethod
    async def _navigate_to_chat(self, page: Any) -> bool:
        """
        导航到聊天页面

        Args:
            page: 浏览器页面对象

        Returns:
            是否导航成功
        """
        pass

    @abstractmethod
    async def _find_input_element(self, page: Any) -> Optional[Any]:
        """
        查找输入框元素

        Args:
            page: 浏览器页面对象

        Returns:
            输入框元素，如果未找到返回 None
        """
        pass

    @abstractmethod
    async def _find_send_button(self, page: Any) -> Optional[Any]:
        """
        查找发送按钮

        Args:
            page: 浏览器页面对象

        Returns:
            发送按钮元素，如果未找到返回 None
        """
        pass

    @abstractmethod
    async def _get_reply_content(self, page: Any) -> Optional[str]:
        """
        获取 AI 回复内容

        Args:
            page: 浏览器页面对象

        Returns:
            回复内容，如果还在生成中返回 None
        """
        pass

    @abstractmethod
    async def _is_generating(self, page: Any) -> bool:
        """
        检查是否正在生成回复

        Args:
            page: 浏览器页面对象

        Returns:
            是否正在生成
        """
        pass

    @abstractmethod
    async def _dismiss_popup(self, page: Any) -> bool:
        """
        关闭弹窗（如登录弹窗）

        Args:
            page: 浏览器页面对象

        Returns:
            是否成功关闭弹窗
        """
        pass

    # ── 抽象方法：高级功能 ──

    @abstractmethod
    async def _select_image_mode(self, page: Any, aspect_ratio: Optional[str] = None) -> bool:
        """
        切换到图片生成模式

        Args:
            page: 浏览器页面对象
            aspect_ratio: 图片比例，如 "16:9", "1:1"

        Returns:
            是否切换成功
        """
        pass

    @abstractmethod
    async def _upload_file(self, page: Any, file_path: str) -> bool:
        """
        上传文件

        Args:
            page: 浏览器页面对象
            file_path: 文件路径

        Returns:
            是否上传成功
        """
        pass

    # ── 默认配置 ──

    @classmethod
    @abstractmethod
    def get_default_config(cls) -> ClientConfig:
        """获取默认配置"""
        pass

    # ── 通用实现 ──

    async def _ensure_ready(self) -> bool:
        """确保客户端就绪"""
        if self._is_ready and await self._check_alive():
            return True
        await self.close()
        return await self.start()

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()

    async def chat(self, message: str, timeout: Optional[int] = None) -> ChatResponse:
        """
        发送消息并获取回复

        Args:
            message: 要发送的消息
            timeout: 超时时间（秒），如果为 None 则使用配置中的超时时间

        Returns:
            ChatResponse 对象

        Raises:
            RuntimeError: 如果客户端未就绪
        """
        if not await self._ensure_ready():
            return ChatResponse(
                content="",
                success=False,
                error=f"客户端未就绪: {self.site_name}"
            )

        timeout = timeout or self._config.timeout

        async with self._lock:
            try:
                # 获取页面
                page = await self._acquire_page()
                if not page:
                    return ChatResponse(
                        content="",
                        success=False,
                        error="无法获取可用页面"
                    )

                # 导航到聊天页面
                if not await self._navigate_to_chat(page):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="导航到聊天页面失败"
                    )

                # 关闭可能的弹窗
                await self._dismiss_popup(page)

                # 查找输入框
                input_el = await self._find_input_element(page)
                if not input_el:
                    return ChatResponse(
                        content="",
                        success=False,
                        error="未找到输入框"
                    )

                # 输入消息
                await self._input_text(page, input_el, message)

                # 发送消息
                if not await self._send_message(page, input_el):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="发送消息失败"
                    )

                # 处理发送后的弹窗
                await asyncio.sleep(1)
                await self._dismiss_popup(page)

                # 等待回复
                return await self._wait_for_reply(page, message, timeout)

            except Exception as e:
                log.error(f"[{self.site_name}] 聊天失败: {e}")
                return ChatResponse(
                    content="",
                    success=False,
                    error=str(e)
                )

    async def chat_stream(self, message: str, timeout: Optional[int] = None) -> AsyncIterator[str]:
        """
        流式发送消息并获取回复

        Args:
            message: 要发送的消息
            timeout: 超时时间（秒）

        Yields:
            回复内容的片段

        Raises:
            RuntimeError: 如果客户端未就绪
        """
        if not await self._ensure_ready():
            return

        timeout = timeout or self._config.timeout

        async with self._lock:
            try:
                # 获取页面
                page = await self._acquire_page()
                if not page:
                    return

                # 导航到聊天页面
                if not await self._navigate_to_chat(page):
                    return

                # 关闭可能的弹窗
                await self._dismiss_popup(page)

                # 查找输入框
                input_el = await self._find_input_element(page)
                if not input_el:
                    return

                # 输入消息
                await self._input_text(page, input_el, message)

                # 发送消息
                if not await self._send_message(page, input_el):
                    return

                # 处理发送后的弹窗
                await asyncio.sleep(1)
                await self._dismiss_popup(page)

                # 流式等待回复
                last_content = ""
                start_time = asyncio.get_event_loop().time()

                while True:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout:
                        break

                    content = await self._get_reply_content(page)

                    # 过滤掉用户消息
                    if content and content.strip() == message.strip():
                        content = None

                    if content and content != last_content:
                        # 返回新增的部分
                        new_part = content[len(last_content):]
                        if new_part:
                            yield new_part
                        last_content = content

                    # 检查是否生成完成
                    if content and not await self._is_generating(page):
                        # 内容稳定检查
                        await asyncio.sleep(1)
                        final_content = await self._get_reply_content(page)
                        if final_content == content:
                            break

                    await asyncio.sleep(0.5)

            except Exception as e:
                log.error(f"[{self.site_name}] 流式聊天失败: {e}")

    async def generate_image(
        self,
        prompt: str,
        aspect_ratio: Optional[str] = None,
        timeout: int = 180
    ) -> ChatResponse:
        """
        生成图片

        Args:
            prompt: 图片生成提示词
            aspect_ratio: 图片比例，如 "16:9", "1:1"
            timeout: 超时时间（秒）

        Returns:
            ChatResponse 对象，images 字段包含生成的图片 URL
        """
        if not await self._ensure_ready():
            return ChatResponse(
                content="",
                success=False,
                error=f"客户端未就绪: {self.site_name}"
            )

        async with self._lock:
            try:
                # 获取页面
                page = await self._acquire_page()
                if not page:
                    return ChatResponse(
                        content="",
                        success=False,
                        error="无法获取可用页面"
                    )

                # 导航到聊天页面
                if not await self._navigate_to_chat(page):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="导航到聊天页面失败"
                    )

                # 关闭可能的弹窗
                await self._dismiss_popup(page)

                # 切换到图片生成模式
                if not await self._select_image_mode(page, aspect_ratio):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="切换到图片生成模式失败"
                    )

                # 查找输入框
                input_el = await self._find_input_element(page)
                if not input_el:
                    return ChatResponse(
                        content="",
                        success=False,
                        error="未找到输入框"
                    )

                # 输入提示词
                await self._input_text(page, input_el, prompt)

                # 发送请求
                if not await self._send_message(page, input_el):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="发送请求失败"
                    )

                # 处理发送后的弹窗
                await asyncio.sleep(1)
                await self._dismiss_popup(page)

                # 等待图片生成
                return await self._wait_for_image(page, timeout)

            except Exception as e:
                log.error(f"[{self.site_name}] 图片生成失败: {e}")
                return ChatResponse(
                    content="",
                    success=False,
                    error=str(e)
                )

    async def upload_and_chat(
        self,
        file_path: str,
        message: str,
        timeout: Optional[int] = None
    ) -> ChatResponse:
        """
        上传文件并发送消息

        Args:
            file_path: 文件路径
            message: 要发送的消息
            timeout: 超时时间（秒）

        Returns:
            ChatResponse 对象
        """
        if not await self._ensure_ready():
            return ChatResponse(
                content="",
                success=False,
                error=f"客户端未就绪: {self.site_name}"
            )

        timeout = timeout or self._config.timeout

        async with self._lock:
            try:
                # 获取页面
                page = await self._acquire_page()
                if not page:
                    return ChatResponse(
                        content="",
                        success=False,
                        error="无法获取可用页面"
                    )

                # 导航到聊天页面
                if not await self._navigate_to_chat(page):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="导航到聊天页面失败"
                    )

                # 关闭可能的弹窗
                await self._dismiss_popup(page)

                # 上传文件
                if not await self._upload_file(page, file_path):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="上传文件失败"
                    )

                # 查找输入框
                input_el = await self._find_input_element(page)
                if not input_el:
                    return ChatResponse(
                        content="",
                        success=False,
                        error="未找到输入框"
                    )

                # 输入消息
                await self._input_text(page, input_el, message)

                # 发送消息
                if not await self._send_message(page, input_el):
                    return ChatResponse(
                        content="",
                        success=False,
                        error="发送消息失败"
                    )

                # 处理发送后的弹窗
                await asyncio.sleep(1)
                await self._dismiss_popup(page)

                # 等待回复
                return await self._wait_for_reply(page, message, timeout)

            except Exception as e:
                log.error(f"[{self.site_name}] 上传并聊天失败: {e}")
                return ChatResponse(
                    content="",
                    success=False,
                    error=str(e)
                )

    # ── 内部辅助方法 ──

    async def _acquire_page(self) -> Optional[Any]:
        """获取一个可用的页面（子类可以覆盖以实现页签池）"""
        return self._page

    async def _input_text(self, page: Any, input_el: Any, text: str) -> bool:
        """
        输入文本到输入框（子类可以覆盖以适配不同的输入框类型）

        Args:
            page: 浏览器页面对象
            input_el: 输入框元素
            text: 要输入的文本

        Returns:
            是否输入成功
        """
        try:
            await input_el.click()
            await asyncio.sleep(0.3)

            # 清空现有内容
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await asyncio.sleep(0.1)

            # 尝试使用 fill
            try:
                await input_el.fill(text)
            except Exception:
                # 回退到键盘输入
                await page.keyboard.type(text, delay=30)

            return True
        except Exception as e:
            log.error(f"输入文本失败: {e}")
            return False

    async def _send_message(self, page: Any, input_el: Any) -> bool:
        """
        发送消息（子类可以覆盖以适配不同的发送方式）

        Args:
            page: 浏览器页面对象
            input_el: 输入框元素

        Returns:
            是否发送成功
        """
        try:
            # 尝试点击发送按钮
            send_btn = await self._find_send_button(page)
            if send_btn:
                try:
                    await send_btn.click()
                    return True
                except Exception:
                    pass

            # 回退到 Enter 键
            await page.keyboard.press("Enter")
            return True
        except Exception as e:
            log.error(f"发送消息失败: {e}")
            return False

    async def _wait_for_reply(
        self,
        page: Any,
        user_message: str,
        timeout: int
    ) -> ChatResponse:
        """
        等待 AI 回复

        Args:
            page: 浏览器页面对象
            user_message: 用户发送的消息（用于过滤）
            timeout: 超时时间（秒）

        Returns:
            ChatResponse 对象
        """
        start_time = asyncio.get_event_loop().time()
        last_content = ""
        stable_count = 0

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                return ChatResponse(
                    content=last_content,
                    success=False,
                    error=f"等待回复超时 ({timeout}s)"
                )

            content = await self._get_reply_content(page)

            # 过滤掉用户消息
            if content and content.strip() == user_message.strip():
                content = None

            if content and content != last_content:
                last_content = content
                stable_count = 0
            elif content and content == last_content:
                stable_count += 1
                # 内容稳定 3 次认为生成完成
                if stable_count >= 3:
                    return ChatResponse(
                        content=content,
                        success=True
                    )
            elif not content and not await self._is_generating(page):
                # 没有内容且不在生成中，可能是空回复
                if elapsed > 10:  # 给一些时间开始生成
                    return ChatResponse(
                        content="",
                        success=False,
                        error="未获取到回复"
                    )

            await asyncio.sleep(1)

    async def _wait_for_image(self, page: Any, timeout: int) -> ChatResponse:
        """
        等待图片生成

        Args:
            page: 浏览器页面对象
            timeout: 超时时间（秒）

        Returns:
            ChatResponse 对象，images 字段包含生成的图片 URL
        """
        start_time = asyncio.get_event_loop().time()

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                return ChatResponse(
                    content="",
                    success=False,
                    error=f"图片生成超时 ({timeout}s)"
                )

            # 检查是否有生成的图片
            images = await self._get_generated_images(page)
            if images:
                return ChatResponse(
                    content=f"成功生成 {len(images)} 张图片",
                    success=True,
                    images=images
                )

            # 检查是否还在生成
            if not await self._is_generating(page) and elapsed > 30:
                return ChatResponse(
                    content="",
                    success=False,
                    error="图片生成失败"
                )

            await asyncio.sleep(3)

    async def _get_generated_images(self, page: Any) -> List[str]:
        """
        获取生成的图片 URL（子类应该覆盖此方法）

        Args:
            page: 浏览器页面对象

        Returns:
            图片 URL 列表
        """
        return []

    async def _debug_screenshot(self, page: Any, name: str) -> None:
        """调试截图"""
        if not self._config.debug_screenshots:
            return

        try:
            screenshot_dir = self._config.screenshot_dir or os.path.join(
                os.path.dirname(__file__), "..", "..", ".."
            )
            os.makedirs(screenshot_dir, exist_ok=True)
            path = os.path.join(screenshot_dir, f"debug_{self.site_name}_{name}.png")
            await page.screenshot(path=path)
            log.debug(f"[{self.site_name}] 截图: {path}")
        except Exception:
            pass
