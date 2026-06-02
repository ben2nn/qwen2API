"""
Qianwen 客户端 (www.qianwen.com)

通义千问国内版客户端，通过 Camoufox 浏览器自动化访问。
"""

import asyncio
import logging
import os
from typing import Optional, List, Any

from .base_client import BaseBrowserChatClient, ChatResponse, ClientConfig

log = logging.getLogger("web2api.browser_chat.qianwen")

# 默认配置
_DEFAULT_CONFIG = ClientConfig(
    headless=True,
    pool_size=5,
    timeout=120,
    site_url="https://www.qianwen.com",
    guest_url="https://www.qianwen.com/",
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


class QianwenClient(BaseBrowserChatClient):
    """
    Qianwen 客户端 (www.qianwen.com)

    通义千问国内版，支持：
    - 文本聊天
    - 图片生成
    - 文件上传
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
        return "qianwen.com"

    @property
    def site_url(self) -> str:
        return "https://www.qianwen.com"

    @classmethod
    def get_default_config(cls) -> ClientConfig:
        return _DEFAULT_CONFIG

    # ── 生命周期 ──

    async def start(self, retries: int = 3) -> bool:
        """启动浏览器并预热页签池"""
        for attempt in range(retries):
            try:
                log.info(f"[Qianwen] 启动 Camoufox... (第 {attempt + 1}/{retries} 次)")
                from camoufox.async_api import AsyncCamoufox

                opts = {**_CAMOUFOX_OPTS, "headless": self._config.headless}
                self._camoufox = AsyncCamoufox(**opts)
                self._browser = await self._camoufox.__aenter__()

                # 预热页签池
                await self._warm_up_pool()

                if self._browser and self._browser.is_connected() and self._tab_pool:
                    self._is_ready = True
                    log.info(f"[Qianwen] ✓ 就绪 (页签: {len(self._tab_pool)}/{self._pool_size})")
                    return True
                else:
                    raise Exception("浏览器启动失败")

            except Exception as e:
                log.warning(f"[Qianwen] 启动失败 (第 {attempt + 1}/{retries} 次): {e}")
                await self.close()
                if attempt < retries - 1:
                    await asyncio.sleep(2)

        log.error("[Qianwen] ✗ 所有启动尝试均失败")
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
        log.info("[Qianwen] 已关闭")

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
                log.warning("[Qianwen] 浏览器不可用，无法创建页签")
                return None
            page = await self._browser.new_page()
            page.on("pageerror", lambda _: None)
            log.info("[Qianwen] 新页签已创建")
            return page
        except Exception as e:
            log.warning(f"[Qianwen] 创建页签失败: {e}")
            return None

    async def _warm_up_pool(self) -> None:
        """串行创建页签池"""
        log.info(f"[Qianwen] 预热页签池 (目标: {self._pool_size})")
        for _ in range(self._pool_size):
            page = await self._create_tab()
            if page:
                self._tab_pool.append(page)
        log.info(f"[Qianwen] 页签池预热完成 (可用: {len(self._tab_pool)}/{self._pool_size})")

    async def _acquire_page(self) -> Optional[Any]:
        """轮询获取下一个页签并导航到主页"""
        if not self._tab_pool:
            log.info("[Qianwen] 页签池为空，创建新页签")
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
            log.warning(f"[Qianwen] 页签 {idx} 已失效，重建")
            page = await self._create_tab()
            if not page:
                return None
            self._tab_pool[idx] = page

        # 导航到主页
        try:
            await page.goto(self._config.guest_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await self._dismiss_popup(page)
            self._page = page
            log.info(f"[Qianwen] 页签已就绪: {page.url} (idx={idx})")
            return page

        except Exception as e:
            log.warning(f"[Qianwen] 导航失败 ({e})，重建页签 {idx}")
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
                    await asyncio.sleep(2)
                    await self._dismiss_popup(new_page)
                    self._page = new_page
                    log.info(f"[Qianwen] 页签 {idx} 重建成功")
                    return new_page
                except Exception as e2:
                    log.error(f"[Qianwen] 重建后导航仍失败: {e2}")
            return None

    # ── 页面交互 ──

    async def _navigate_to_chat(self, page: Any) -> bool:
        """导航到聊天页面（已在 _acquire_page 中完成）"""
        return True

    async def _dismiss_popup(self, page: Any) -> bool:
        """关闭登录弹窗"""
        close_selectors = [
            'button:has-text("Stay logged out")',
            'button:has-text("保持登出")',
            'button:has-text("保持未登录")',
            'button:has-text("Continue without login")',
            'button:has-text("无需登录")',
            'button:has-text("暂不登录")',
            'button:has-text("不用了")',
            'button:has-text("Skip")',
            'button:has-text("Later")',
            'button:has-text("关闭")',
            'button:has-text("Close")',
        ]

        for sel in close_selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    log.info(f"[Qianwen] 已点击: {sel}")
                    await asyncio.sleep(2)
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
                log.info("[Qianwen] 使用 ESC 关闭弹窗")
                return True
        except Exception:
            pass

        return False

    async def _find_input_element(self, page: Any) -> Optional[Any]:
        """查找输入框"""
        selectors = [
            'textarea',
            '[contenteditable="true"]',
            'div[role="textbox"]',
            'input[type="text"]',
            '[placeholder]',
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
        """查找发送按钮"""
        selectors = [
            'button[type="submit"]',
            'button[aria-label*="send" i]',
            'button[aria-label*="Send" i]',
            'button[aria-label*="发送"]',
        ]
        for sel in selectors:
            try:
                btn = await page.query_selector(sel)
                if btn and await btn.is_visible() and not await btn.evaluate("el => el.disabled"):
                    return btn
            except Exception:
                pass
        return None

    async def _get_reply_content(self, page: Any) -> Optional[str]:
        """获取 AI 回复内容"""
        try:
            return await page.evaluate(r"""() => {
                const candidates = [];
                const selectors = [
                    '[class*="markdown"]',
                    '[class*="message-content"]',
                    '[class*="markdown-body"]',
                    '[class*="message"]:last-child',
                    '[class*="response"]:last-child',
                    '[class*="answer"]:last-child',
                    '[class*="assistant"]:last-child',
                    'div[role="assistant"]:last-child',
                    '.chat-message:last-child',
                ];

                for (let i = 0; i < selectors.length; i++) {
                    const sel = selectors[i];
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        if (el.offsetParent === null) continue;
                        const text = el.textContent?.trim();
                        if (!text || text.length <= 5) continue;
                        if (text === '你好' || text === '有什么我能帮您的吗？') continue;
                        if (text.includes('有什么我能帮') && text.length < 50) continue;
                        candidates.push({ text: text.substring(0, 2000), priority: i });
                    }
                }

                candidates.sort((a, b) => a.priority - b.priority);
                if (candidates.length > 0) return candidates[0].text;

                // 兜底
                const allDivs = document.querySelectorAll('div');
                for (let i = allDivs.length - 1; i >= 0; i--) {
                    const el = allDivs[i];
                    if (el.offsetParent === null) continue;
                    const t = el.textContent?.trim();
                    if (t && t.length > 20 && !t.includes('qianwen') && !t.includes('千问'))
                        return t.substring(0, 2000);
                }
                return null;
            }""")
        except Exception:
            return None

    async def _is_generating(self, page: Any) -> bool:
        """检查是否正在生成"""
        try:
            return await page.evaluate("""() => {
                const stop = document.querySelector('button[aria-label="Stop"], button[aria-label="停止"]');
                const loading = document.querySelector('[class*="loading"], [class*="generating"], [class*="spinner"]');
                return !!(stop || loading);
            }""")
        except Exception:
            return False

    async def _select_image_mode(self, page: Any, aspect_ratio: Optional[str] = None) -> bool:
        """切换到图片生成模式"""
        # 等待页面就绪
        try:
            await page.wait_for_selector('textarea, [contenteditable="true"]', timeout=15000)
        except Exception:
            pass

        # 方法 1：点击 "更多" 按钮展开菜单
        more_btn = None
        for sel in ['button[aria-label="更多"]', 'button:has-text("更多")']:
            try:
                btns = await page.query_selector_all(sel)
                for btn in btns:
                    if await btn.is_visible():
                        more_btn = btn
                        break
                if more_btn:
                    break
            except Exception:
                pass

        if more_btn:
            await more_btn.click()
            await asyncio.sleep(1)

            # 在展开的菜单中查找 "AI生图"
            for sel in ['button:has-text("AI生图")', '[aria-label="AI生图"]']:
                try:
                    item = await page.wait_for_selector(sel, timeout=3000, state='visible')
                    if item:
                        await item.click()
                        log.info(f"[Qianwen] 已选择 AI生图: {sel}")
                        await asyncio.sleep(2)
                        return True
                except Exception:
                    pass

        # 方法 2：JavaScript 点击
        try:
            clicked = await page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent?.trim() || '';
                    const aria = btn.getAttribute('aria-label') || '';
                    if (text.includes('AI生图') || aria.includes('AI生图')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if clicked:
                log.info("[Qianwen] 通过 JavaScript 点击 AI生图 按钮")
                await asyncio.sleep(2)
                return True
        except Exception as e:
            log.warning(f"[Qianwen] JavaScript 点击失败: {e}")

        log.warning("[Qianwen] 未找到图片生成选项")
        return False

    async def _upload_file(self, page: Any, file_path: str) -> bool:
        """上传文件"""
        if not os.path.exists(file_path):
            log.error(f"[Qianwen] 文件不存在: {file_path}")
            return False

        abs_path = os.path.abspath(file_path)
        log.info(f"[Qianwen] 准备上传文件: {abs_path}")

        # 等待页面就绪
        try:
            await page.wait_for_selector('textarea, [contenteditable="true"]', timeout=15000)
        except Exception:
            pass

        # 方法 A：点击 "添加附件" 按钮
        for sel in [
            'button[aria-label="添加附件"]',
            'button[aria-label*="附件"]',
            'button[aria-label*="attach" i]',
            'button[aria-label*="upload" i]',
        ]:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000, state='visible')
                if btn:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await btn.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(abs_path)
                    log.info("[Qianwen] 文件上传成功")
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass

        # 方法 B：通过 + 菜单上传
        plus_clicked = False
        for sel in ['.mode-select-open', '.ant-dropdown-trigger', '[class*="plus"]']:
            try:
                btn = await page.wait_for_selector(sel, timeout=3000, state='visible')
                if btn:
                    await btn.click()
                    plus_clicked = True
                    break
            except Exception:
                pass

        if plus_clicked:
            await asyncio.sleep(1)
            for sel in ['[data-menu-id*="upload"]', '.ant-dropdown-menu-item:has-text("上传")', '.ant-dropdown-menu-item:has-text("文件")']:
                try:
                    upload_item = await page.wait_for_selector(sel, timeout=3000, state='visible')
                    if upload_item:
                        async with page.expect_file_chooser(timeout=5000) as fc_info:
                            await upload_item.click()
                        file_chooser = await fc_info.value
                        await file_chooser.set_files(abs_path)
                        log.info("[Qianwen] 文件上传成功")
                        await asyncio.sleep(2)
                        return True
                except Exception:
                    pass

        # 方法 C：直接设置 file input
        try:
            upload_input = await page.wait_for_selector('input[type="file"]', timeout=5000)
            if upload_input:
                await upload_input.set_input_files(abs_path)
                log.info("[Qianwen] 通过 file input 上传成功")
                await asyncio.sleep(2)
                return True
        except Exception as e:
            log.warning(f"[Qianwen] file input 上传失败: {e}")

        return False

    async def _get_generated_images(self, page: Any) -> List[str]:
        """获取生成的图片 URL"""
        try:
            result = await page.evaluate(r"""() => {
                const images = [];
                const seen = new Set();

                const isPlaceholder = (src) => {
                    if (!src) return true;
                    if (src.match(/\.(png|jpg|jpeg|webp)\?key=/i)) return false;
                    if (src.includes('cdn.') && !src.match(/\.(png|jpg|jpeg|webp)/i)) return true;
                    return false;
                };

                // 检查特定容器
                document.querySelectorAll('.qwen-markdown-image-content, .ant-image').forEach(container => {
                    const img = container.querySelector('img');
                    if (img && img.src && !isPlaceholder(img.src) && !seen.has(img.src)) {
                        seen.add(img.src);
                        images.push(img.src);
                    }
                });

                // 兜底
                if (images.length === 0) {
                    document.querySelectorAll('img').forEach(img => {
                        if (img.src && !isPlaceholder(img.src) && !seen.has(img.src) && img.naturalWidth > 100) {
                            seen.add(img.src);
                            images.push(img.src);
                        }
                    });
                }

                return images;
            }""")
            return result or []
        except Exception:
            return []
