"""
浏览器自动化聊天服务

通过 Playwright 浏览器自动化操作 Qwen 网页版，
模拟真实用户行为获取 AI 回复。
"""
import asyncio
import logging
import time
from typing import AsyncIterator, Optional
from dataclasses import dataclass

log = logging.getLogger("web2api.browser_chat")

BASE_URL = "https://chat.qwen.ai"


@dataclass
class BrowserChatResponse:
    """浏览器聊天响应"""
    content: str
    reasoning: str = ""
    success: bool = True
    error: str = ""


class BrowserChatService:
    """浏览器自动化聊天服务"""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None
        self._is_initialized = False

    async def initialize(self) -> bool:
        """初始化浏览器"""
        try:
            log.info("[BrowserChat] 初始化浏览器...")
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

            # 使用 Chromium 浏览器（更稳定）
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ]
            )

            # 创建上下文（模拟真实浏览器）
            context = await self._browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            )

            self._page = await context.new_page()

            # 访问 Qwen 首页
            log.info(f"[BrowserChat] 访问 {BASE_URL}")
            await self._page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)

            self._is_initialized = True
            log.info("[BrowserChat] ✓ 浏览器初始化完成")
            return True
        except Exception as e:
            log.error(f"[BrowserChat] ✗ 浏览器初始化失败: {e}")
            return False

    async def close(self):
        """关闭浏览器"""
        try:
            if self._browser:
                await self._browser.close()
                self._browser = None
                self._page = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            self._is_initialized = False
            log.info("[BrowserChat] 浏览器已关闭")
        except Exception as e:
            log.warning(f"[BrowserChat] 关闭浏览器异常: {e}")

    async def _wait_for_ready(self, timeout: int = 30) -> bool:
        """等待页面就绪"""
        try:
            # 等待输入框出现
            await self._page.wait_for_selector('textarea, [contenteditable="true"]', timeout=timeout * 1000)
            return True
        except Exception as e:
            log.warning(f"[BrowserChat] 等待页面就绪超时: {e}")
            return False

    async def _input_message(self, message: str) -> bool:
        """输入消息"""
        try:
            # 尝试多种输入框选择器
            selectors = [
                'textarea',
                '[contenteditable="true"]',
                'div[role="textbox"]',
                'input[type="text"]',
            ]

            for selector in selectors:
                try:
                    element = await self._page.wait_for_selector(selector, timeout=5000)
                    if element:
                        await element.click()
                        await asyncio.sleep(0.5)

                        # 清空输入框
                        await self._page.keyboard.press('Control+A')
                        await self._page.keyboard.press('Delete')
                        await asyncio.sleep(0.2)

                        # 输入消息
                        await element.fill(message)
                        await asyncio.sleep(0.5)

                        log.info(f"[BrowserChat] ✓ 消息已输入: {message[:50]}...")
                        return True
                except Exception:
                    continue

            log.error("[BrowserChat] ✗ 找不到输入框")
            return False
        except Exception as e:
            log.error(f"[BrowserChat] ✗ 输入消息失败: {e}")
            return False

    async def _send_message(self) -> bool:
        """发送消息"""
        try:
            # 尝试多种发送按钮选择器
            selectors = [
                'button[type="submit"]',
                'button:has-text("Send")',
                'button:has-text("发送")',
                'button[aria-label="Send"]',
                'button[aria-label="发送"]',
                'div[role="button"]:has-text("Send")',
                'div[role="button"]:has-text("发送")',
            ]

            for selector in selectors:
                try:
                    element = await self._page.wait_for_selector(selector, timeout=3000)
                    if element:
                        await element.click()
                        log.info("[BrowserChat] ✓ 消息已发送")
                        return True
                except Exception:
                    continue

            # 尝试按 Enter 键发送
            await self._page.keyboard.press('Enter')
            log.info("[BrowserChat] ✓ 使用 Enter 键发送")
            return True
        except Exception as e:
            log.error(f"[BrowserChat] ✗ 发送消息失败: {e}")
            return False

    async def _wait_for_response(self, timeout: int = 120) -> Optional[str]:
        """等待 AI 回复"""
        try:
            log.info("[BrowserChat] 等待 AI 回复...")

            # 等待回复开始
            await asyncio.sleep(2)

            # 等待回复完成（通过检测停止按钮消失或回复内容稳定）
            start_time = time.time()
            last_content = ""
            stable_count = 0

            while time.time() - start_time < timeout:
                # 检查是否还在生成中
                is_generating = await self._page.evaluate("""
                    () => {
                        // 检查是否有停止按钮
                        const stopBtn = document.querySelector('button[aria-label="Stop"]');
                        if (stopBtn) return true;

                        // 检查是否有加载指示器
                        const loading = document.querySelector('.loading, .generating, [class*="loading"]');
                        if (loading) return true;

                        return false;
                    }
                """)

                if not is_generating:
                    # 获取回复内容
                    content = await self._get_response_content()
                    if content and content == last_content:
                        stable_count += 1
                        if stable_count >= 3:  # 内容稳定3次
                            log.info("[BrowserChat] ✓ AI 回复完成")
                            return content
                    else:
                        stable_count = 0
                        last_content = content

                await asyncio.sleep(1)

            log.warning("[BrowserChat] ✗ 等待回复超时")
            return None
        except Exception as e:
            log.error(f"[BrowserChat] ✗ 等待回复异常: {e}")
            return None

    async def _get_response_content(self) -> Optional[str]:
        """获取回复内容"""
        try:
            # 尝试多种回复内容选择器
            selectors = [
                '.message-content:last-child',
                '[class*="message"]:last-child',
                '[class*="response"]:last-child',
                '[class*="answer"]:last-child',
                'div[role="assistant"]:last-child',
                '.chat-message:last-child',
            ]

            for selector in selectors:
                try:
                    elements = await self._page.query_selector_all(selector)
                    if elements:
                        # 获取最后一个元素的文本
                        content = await elements[-1].text_content()
                        if content and len(content.strip()) > 0:
                            return content.strip()
                except Exception:
                    continue

            # 尝试获取所有文本内容
            try:
                content = await self._page.evaluate("""
                    () => {
                        const messages = document.querySelectorAll('[class*="message"], [class*="response"], [class*="answer"]');
                        if (messages.length > 0) {
                            return messages[messages.length - 1].textContent;
                        }
                        return null;
                    }
                """)
                if content:
                    return content.strip()
            except Exception:
                pass

            return None
        except Exception as e:
            log.error(f"[BrowserChat] 获取回复内容异常: {e}")
            return None

    async def chat(self, message: str, timeout: int = 120) -> BrowserChatResponse:
        """发送消息并获取回复"""
        if not self._is_initialized:
            if not await self.initialize():
                return BrowserChatResponse(
                    content="",
                    success=False,
                    error="浏览器初始化失败"
                )

        try:
            # 等待页面就绪
            if not await self._wait_for_ready():
                return BrowserChatResponse(
                    content="",
                    success=False,
                    error="页面未就绪"
                )

            # 输入消息
            if not await self._input_message(message):
                return BrowserChatResponse(
                    content="",
                    success=False,
                    error="输入消息失败"
                )

            # 发送消息
            if not await self._send_message():
                return BrowserChatResponse(
                    content="",
                    success=False,
                    error="发送消息失败"
                )

            # 等待回复
            response = await self._wait_for_response(timeout)
            if response:
                return BrowserChatResponse(
                    content=response,
                    success=True
                )
            else:
                return BrowserChatResponse(
                    content="",
                    success=False,
                    error="获取回复超时"
                )

        except Exception as e:
            log.error(f"[BrowserChat] 聊天异常: {e}")
            return BrowserChatResponse(
                content="",
                success=False,
                error=str(e)
            )

    async def chat_stream(self, message: str, timeout: int = 120) -> AsyncIterator[dict]:
        """流式聊天（简化版本，实际是轮询获取内容）"""
        if not self._is_initialized:
            if not await self.initialize():
                yield {"error": "浏览器初始化失败"}
                return

        try:
            # 等待页面就绪
            if not await self._wait_for_ready():
                yield {"error": "页面未就绪"}
                return

            # 输入消息
            if not await self._input_message(message):
                yield {"error": "输入消息失败"}
                return

            # 发送消息
            if not await self._send_message():
                yield {"error": "发送消息失败"}
                return

            # 流式获取回复
            start_time = time.time()
            last_content = ""

            while time.time() - start_time < timeout:
                # 检查是否还在生成
                is_generating = await self._page.evaluate("""
                    () => {
                        const stopBtn = document.querySelector('button[aria-label="Stop"]');
                        return !!stopBtn;
                    }
                """)

                # 获取当前内容
                content = await self._get_response_content()
                if content and content != last_content:
                    # 发送增量内容
                    new_content = content[len(last_content):]
                    if new_content:
                        yield {"content": new_content}
                    last_content = content

                if not is_generating and content:
                    # 生成完成
                    yield {"done": True}
                    return

                await asyncio.sleep(0.5)

            yield {"error": "回复超时"}

        except Exception as e:
            log.error(f"[BrowserChat] 流式聊天异常: {e}")
            yield {"error": str(e)}


# 全局实例
_browser_chat_service: Optional[BrowserChatService] = None


async def get_browser_chat_service() -> BrowserChatService:
    """获取浏览器聊天服务实例"""
    global _browser_chat_service
    if _browser_chat_service is None:
        _browser_chat_service = BrowserChatService()
    return _browser_chat_service


async def close_browser_chat_service():
    """关闭浏览器聊天服务"""
    global _browser_chat_service
    if _browser_chat_service:
        await _browser_chat_service.close()
        _browser_chat_service = None