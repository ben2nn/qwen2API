"""
Doubao 客户端 (www.doubao.com)

豆包 AI 客户端，通过 Camoufox 浏览器自动化访问。

注意：此客户端需要根据实际页面结构进行调整。
"""

import asyncio
import logging
import os
from typing import Optional, List, Any

from .base_client import BaseBrowserChatClient, ChatResponse, ClientConfig

log = logging.getLogger("web2api.browser_chat.doubao")

# 默认配置
_DEFAULT_CONFIG = ClientConfig(
    headless=True,
    pool_size=5,
    timeout=120,
    site_url="https://www.doubao.com",
    guest_url="https://www.doubao.com/chat/",
)

# Camoufox 配置
_CAMOUFOX_OPTS = {
    "headless": True,
    "humanize": False,
    "i_know_what_im_doing": True,
    "firefox_user_prefs": {
        "layers.acceleration.disabled": True,
        "gfx.webrender.enabled": False,
        "gfx.webrender.all": False,
        "gfx.webrender.software": False,
        "gfx.canvas.azure.backends": "skia",
        "media.hardware-video-decoding.enabled": False,
    },
}


class DoubaoClient(BaseBrowserChatClient):
    """
    Doubao 客户端 (www.doubao.com)

    豆包 AI 客户端，支持：
    - 文本聊天
    - 图片生成（待实现）
    - 文件上传（待实现）

    注意：此客户端需要根据实际页面结构调整选择器。
    """

    def __init__(self, config: Optional[ClientConfig] = None, pool_size: int = 5):
        """
        初始化客户端

        Args:
            config: 客户端配置
            pool_size: 页签池大小
        """
        super().__init__(config or _DEFAULT_CONFIG)
        self._pool_size = pool_size
        self._camoufox = None

    @property
    def site_name(self) -> str:
        return "doubao.com"

    @property
    def site_url(self) -> str:
        return "https://www.doubao.com"

    @classmethod
    def get_default_config(cls) -> ClientConfig:
        return _DEFAULT_CONFIG

    # ── 生命周期 ──

    async def start(self, retries: int = 3) -> bool:
        """启动浏览器并预热页签池"""
        for attempt in range(retries):
            try:
                log.info(f"[Doubao] 启动 Camoufox... (第 {attempt + 1}/{retries} 次)")
                from camoufox.async_api import AsyncCamoufox

                opts = {**_CAMOUFOX_OPTS, "headless": self._config.headless}
                self._camoufox = AsyncCamoufox(**opts)
                self._browser = await self._camoufox.__aenter__()

                # 预热页签池
                await self._warm_up_pool()

                if self._browser and self._browser.is_connected() and self._tab_pool:
                    self._is_ready = True
                    log.info(f"[Doubao] ✓ 就绪 (页签: {len(self._tab_pool)}/{self._pool_size})")
                    return True
                else:
                    raise Exception("浏览器启动失败")

            except Exception as e:
                log.warning(f"[Doubao] 启动失败 (第 {attempt + 1}/{retries} 次): {e}")
                await self.close()
                if attempt < retries - 1:
                    await asyncio.sleep(2)

        log.error("[Doubao] ✗ 所有启动尝试均失败")
        return False

    async def close(self) -> None:
        """关闭浏览器和所有页签"""
        for page in self._tab_pool:
            try:
                await page.close()
            except Exception:
                pass
        self._tab_pool.clear()

        try:
            if self._camoufox:
                await self._camoufox.__aexit__(None, None, None)
        except Exception:
            pass

        self._browser = None
        self._camoufox = None
        self._page = None
        self._is_ready = False
        log.info("[Doubao] 已关闭")

    async def _check_alive(self) -> bool:
        """检查浏览器是否存活"""
        if not self._browser:
            return False
        try:
            if self._tab_pool:
                await asyncio.wait_for(self._tab_pool[0].evaluate("() => true"), timeout=3)
                return True
            return self._browser.is_connected()
        except Exception:
            return False

    # ── 页签池管理 ──

    async def _create_tab(self) -> Optional[Any]:
        """创建一个新页签"""
        try:
            if not self._browser or not self._browser.is_connected():
                log.warning("[Doubao] 浏览器不可用，无法创建页签")
                return None
            page = await self._browser.new_page()
            page.on("pageerror", lambda _: None)
            log.info("[Doubao] 新页签已创建")
            return page
        except Exception as e:
            log.warning(f"[Doubao] 创建页签失败: {e}")
            return None

    async def _warm_up_pool(self) -> None:
        """串行创建页签池"""
        log.info(f"[Doubao] 预热页签池 (目标: {self._pool_size})")
        for _ in range(self._pool_size):
            page = await self._create_tab()
            if page:
                self._tab_pool.append(page)
        log.info(f"[Doubao] 页签池预热完成 (可用: {len(self._tab_pool)}/{self._pool_size})")

    async def _acquire_page(self) -> Optional[Any]:
        """轮询获取下一个页签并导航到聊天页面"""
        if not self._tab_pool:
            log.info("[Doubao] 页签池为空，创建新页签")
            page = await self._create_tab()
            if not page:
                return None
            self._tab_pool.append(page)

        # 轮询取下一个页签
        idx = self._current_idx % len(self._tab_pool)
        self._current_idx = idx + 1
        page = self._tab_pool[idx]

        # 检查页签是否仍然可用
        try:
            await asyncio.wait_for(page.evaluate("() => true"), timeout=3)
        except Exception:
            log.warning(f"[Doubao] 页签 {idx} 已失效，重建")
            page = await self._create_tab()
            if not page:
                return None
            self._tab_pool[idx] = page

        # 导航到聊天页面
        try:
            await page.goto(self._config.guest_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            await self._dismiss_popup(page)
            self._page = page
            log.info(f"[Doubao] 页签已就绪: {page.url} (idx={idx})")
            return page

        except Exception as e:
            log.warning(f"[Doubao] 导航失败 ({e})，重建页签 {idx}")
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass

            new_page = await self._create_tab()
            if new_page:
                self._tab_pool[idx] = new_page
                try:
                    await new_page.goto(self._config.guest_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                    await self._dismiss_popup(new_page)
                    self._page = new_page
                    log.info(f"[Doubao] 页签 {idx} 重建成功")
                    return new_page
                except Exception as e2:
                    log.error(f"[Doubao] 重建后导航仍失败: {e2}")
            return None

    # ── 页面交互 ──

    async def _navigate_to_chat(self, page: Any) -> bool:
        """导航到聊天页面（已在 _acquire_page 中完成）"""
        return True

    async def _dismiss_popup(self, page: Any) -> bool:
        """关闭弹窗（如登录弹窗、提示弹窗等）

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        """
        # 通用弹窗关闭选择器
        close_selectors = [
            'button:has-text("关闭")',
            'button:has-text("Close")',
            'button:has-text("跳过")',
            'button:has-text("Skip")',
            'button:has-text("稍后")',
            'button:has-text("Later")',
            'button:has-text("不用了")',
            'button:has-text("取消")',
            'button:has-text("Cancel")',
            '[aria-label="close"]',
            '[aria-label="Close"]',
            '[class*="close"]',
        ]

        for sel in close_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.info(f"[Doubao] 已点击: {sel}")
                    await asyncio.sleep(1)
                    return True
            except Exception:
                pass

        # ESC 兜底
        try:
            is_modal = await page.evaluate("""() => {
                const modals = document.querySelectorAll('[role="dialog"], [class*="modal"], [class*="dialog"], [class*="popup"]');
                for (const m of modals) { if (m.offsetParent !== null) return true; }
                return false;
            }""")
            if is_modal:
                await page.keyboard.press('Escape')
                await asyncio.sleep(1)
                log.info("[Doubao] 使用 ESC 关闭弹窗")
                return True
        except Exception:
            pass

        return False

    async def _find_input_element(self, page: Any) -> Optional[Any]:
        """查找输入框

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        """
        selectors = [
            'textarea',
            '[contenteditable="true"]',
            'div[role="textbox"]',
            'input[type="text"]',
            '[placeholder]',
            'div[class*="input"]',
            'div[class*="editor"]',
            'div[class*="chat-input"]',
        ]
        for sel in selectors:
            try:
                elements = await page.query_selector_all(sel)
                for el in elements:
                    if await el.is_visible():
                        return el
            except Exception:
                pass
        return None

    async def _find_send_button(self, page: Any) -> Optional[Any]:
        """查找发送按钮

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        """
        selectors = [
            'button[type="submit"]',
            'button[aria-label*="send" i]',
            'button[aria-label*="Send" i]',
            'button[aria-label*="发送"]',
            'button[class*="send"]',
            'button[class*="submit"]',
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    return btn
            except Exception:
                pass
        return None

    async def _get_reply_content(self, page: Any) -> Optional[str]:
        """获取 AI 回复内容

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        """
        try:
            return await page.evaluate(r"""() => {
                const candidates = [];

                // 通用选择器
                const selectors = [
                    '[class*="markdown"]',
                    '[class*="message-content"]',
                    '[class*="markdown-body"]',
                    '[class*="message"]:last-child',
                    '[class*="response"]:last-child',
                    '[class*="answer"]:last-child',
                    '[class*="assistant"]:last-child',
                    '[class*="bot"]:last-child',
                    '[class*="ai"]:last-child',
                    'div[role="assistant"]:last-child',
                ];

                for (let i = 0; i < selectors.length; i++) {
                    const sel = selectors[i];
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        if (el.offsetParent === null) continue;
                        const text = el.textContent?.trim();
                        if (!text || text.length <= 5) continue;
                        candidates.push({ text: text.substring(0, 2000), priority: i });
                    }
                }

                candidates.sort((a, b) => a.priority - b.priority);
                if (candidates.length > 0) return candidates[0].text;

                // 兜底：查找最后出现的较长文本块
                const allDivs = document.querySelectorAll('div');
                for (let i = allDivs.length - 1; i >= 0; i--) {
                    const el = allDivs[i];
                    if (el.offsetParent === null) continue;
                    const t = el.textContent?.trim();
                    if (t && t.length > 20)
                        return t.substring(0, 2000);
                }
                return null;
            }""")
        except Exception:
            return None

    async def _is_generating(self, page: Any) -> bool:
        """检查是否正在生成

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        """
        try:
            return await page.evaluate("""() => {
                // 检查停止按钮
                const stop = document.querySelector(
                    'button[aria-label="Stop"], button[aria-label="停止"], ' +
                    'button[aria-label="stop"], [class*="stop"]'
                );
                if (stop) return true;

                // 检查加载状态
                const loading = document.querySelector(
                    '[class*="loading"], [class*="generating"], [class*="spinner"], ' +
                    '[class*="typing"], [class*="thinking"]'
                );
                if (loading) return true;

                return false;
            }""")
        except Exception:
            return False

    async def _select_image_mode(self, page: Any, aspect_ratio: Optional[str] = None) -> bool:
        """切换到图片生成模式

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        目前返回 False，表示不支持图片生成。
        """
        log.warning("[Doubao] 图片生成功能待实现")
        return False

    async def _upload_file(self, page: Any, file_path: str) -> bool:
        """上传文件

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        """
        if not os.path.exists(file_path):
            log.error(f"[Doubao] 文件不存在: {file_path}")
            return False

        abs_path = os.path.abspath(file_path)
        log.info(f"[Doubao] 准备上传文件: {abs_path}")

        # 等待页面就绪
        try:
            await page.wait_for_selector('textarea, [contenteditable="true"]', timeout=15000)
        except Exception:
            pass

        # 方法 A：查找上传按钮
        for sel in [
            'button[aria-label*="上传"]',
            'button[aria-label*="upload" i]',
            'button[aria-label*="附件"]',
            'button[aria-label*="attach" i]',
            'button:has-text("上传")',
            'button:has-text("附件")',
        ]:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000, state='visible')
                if btn:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await btn.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(abs_path)
                    log.info("[Doubao] 文件上传成功")
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass

        # 方法 B：直接设置 file input
        try:
            upload_input = await page.wait_for_selector('input[type="file"]', timeout=5000)
            if upload_input:
                await upload_input.set_input_files(abs_path)
                log.info("[Doubao] 通过 file input 上传成功")
                await asyncio.sleep(2)
                return True
        except Exception as e:
            log.warning(f"[Doubao] file input 上传失败: {e}")

        log.warning("[Doubao] 未找到上传方式")
        return False

    async def _get_generated_images(self, page: Any) -> List[str]:
        """获取生成的图片 URL

        注意：此方法需要根据 doubao.com 的实际页面结构调整。
        """
        try:
            result = await page.evaluate(r"""() => {
                const images = [];
                const seen = new Set();

                // 检查所有图片
                document.querySelectorAll('img').forEach(img => {
                    if (img.src && !seen.has(img.src) && img.naturalWidth > 100) {
                        // 过滤掉明显的图标和占位图
                        if (!img.src.includes('icon') && !img.src.includes('logo') &&
                            !img.src.includes('avatar') && !img.src.includes('placeholder')) {
                            seen.add(img.src);
                            images.push(img.src);
                        }
                    }
                });

                return images;
            }""")
            return result or []
        except Exception:
            return []
