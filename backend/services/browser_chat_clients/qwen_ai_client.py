"""
Qwen AI 客户端 (chat.qwen.ai)

通过 Camoufox 访问 Qwen 访客模式，
无需登录、无需 token，直接通过页面交互获取 AI 回复。

支持多页签池，并发请求时使用不同页签。
"""

import asyncio
import logging
import os
from typing import Optional, List, Any

from .base_client import BaseBrowserChatClient, ChatResponse, ClientConfig

log = logging.getLogger("web2api.browser_chat.qwen_ai")

# 默认配置
_DEFAULT_CONFIG = ClientConfig(
    headless=True,
    pool_size=5,
    timeout=120,
    site_url="https://chat.qwen.ai",
    guest_url="https://chat.qwen.ai",
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


class QwenAIClient(BaseBrowserChatClient):
    """
    Qwen AI 客户端 (chat.qwen.ai)

    支持：
    - 文本聊天
    - 图片生成
    - 文件上传
    - 多页签池并发
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
        return "qwen.ai"

    @property
    def site_url(self) -> str:
        return "https://chat.qwen.ai"

    @classmethod
    def get_default_config(cls) -> ClientConfig:
        return _DEFAULT_CONFIG

    # ── 生命周期 ──

    async def start(self, retries: int = 3) -> bool:
        """启动浏览器并预热页签池"""
        for attempt in range(retries):
            try:
                log.info(f"[QwenAI] 启动 Camoufox... (第 {attempt + 1}/{retries} 次)")
                from camoufox.async_api import AsyncCamoufox

                opts = {**_CAMOUFOX_OPTS, "headless": self._config.headless}
                self._camoufox = AsyncCamoufox(**opts)
                self._browser = await self._camoufox.__aenter__()

                # 预热页签池
                await self._warm_up_pool()

                if self._browser and self._browser.is_connected() and self._tab_pool:
                    self._is_ready = True
                    log.info(f"[QwenAI] ✓ 就绪 (页签: {len(self._tab_pool)}/{self._pool_size})")
                    return True
                else:
                    raise Exception("浏览器启动失败")

            except Exception as e:
                log.warning(f"[QwenAI] 启动失败 (第 {attempt + 1}/{retries} 次): {e}")
                await self.close()
                if attempt < retries - 1:
                    await asyncio.sleep(2)

        log.error("[QwenAI] ✗ 所有启动尝试均失败")
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
        log.info("[QwenAI] 已关闭")

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
                log.warning("[QwenAI] 浏览器不可用，无法创建页签")
                return None
            page = await self._browser.new_page()
            page.on("pageerror", lambda _: None)
            log.info("[QwenAI] 新页签已创建")
            return page
        except Exception as e:
            log.warning(f"[QwenAI] 创建页签失败: {e}")
            return None

    async def _warm_up_pool(self) -> None:
        """串行创建页签池"""
        log.info(f"[QwenAI] 预热页签池 (目标: {self._pool_size})")
        for _ in range(self._pool_size):
            page = await self._create_tab()
            if page:
                self._tab_pool.append(page)
        log.info(f"[QwenAI] 页签池预热完成 (可用: {len(self._tab_pool)}/{self._pool_size})")

    async def _acquire_page(self) -> Optional[Any]:
        """轮询获取下一个页签并导航到访客页"""
        if not self._tab_pool:
            log.info("[QwenAI] 页签池为空，创建新页签")
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
            log.warning(f"[QwenAI] 页签 {idx} 已失效，重建")
            page = await self._create_tab()
            if not page:
                return None
            self._tab_pool[idx] = page

        # 导航到访客页
        try:
            await page.goto(self._config.guest_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(0.5)

            current_url = page.url
            if '/c/' not in current_url:
                log.warning(f"[QwenAI] 页面被重定向到 {current_url}，重试导航")
                await page.goto(self._config.guest_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.5)

            await self._dismiss_popup(page)
            self._page = page
            log.info(f"[QwenAI] 页签已就绪: {page.url} (idx={idx})")
            return page

        except Exception as e:
            log.warning(f"[QwenAI] 导航失败 ({e})，重建页签 {idx}")
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
                    await asyncio.sleep(0.5)
                    await self._dismiss_popup(new_page)
                    self._page = new_page
                    log.info(f"[QwenAI] 页签 {idx} 重建成功")
                    return new_page
                except Exception as e2:
                    log.error(f"[QwenAI] 重建后导航仍失败: {e2}")
            return None

    # ── 页面交互 ──

    async def _navigate_to_chat(self, page: Any) -> bool:
        """导航到聊天页面（已在 _acquire_page 中完成）"""
        return True

    async def _dismiss_popup(self, page: Any) -> bool:
        """关闭登录弹窗"""
        try:
            result = await asyncio.wait_for(
                page.evaluate(r"""() => {
                    // 检查 qwen-modal-overlay
                    const overlay = document.querySelector('.qwen-modal-overlay');
                    if (overlay && overlay.offsetParent !== null) {
                        const btnTexts = [
                            'Stay logged out', '保持登出', '无需登录', '暂不登录',
                            '不用了', 'Skip', 'Later', 'Close', '关闭',
                        ];
                        const buttons = overlay.querySelectorAll('button, [role="button"]');
                        for (const btn of buttons) {
                            const text = btn.textContent?.trim() || '';
                            for (const target of btnTexts) {
                                if (text.includes(target)) {
                                    btn.click();
                                    return { clicked: target, source: 'overlay' };
                                }
                            }
                        }
                        const firstBtn = overlay.querySelector('button');
                        if (firstBtn) {
                            firstBtn.click();
                            return { clicked: firstBtn.textContent?.trim(), source: 'overlay-first-btn' };
                        }
                        return { clicked: null, hasOverlay: true };
                    }

                    // 检查其他弹窗
                    const btnTexts = [
                        'Stay logged out', '保持登出', '无需登录', '暂不登录',
                        '不用了', 'Skip', 'Later', 'Close', '关闭',
                    ];
                    const buttons = document.querySelectorAll('button');
                    for (const btn of buttons) {
                        const text = btn.textContent?.trim() || '';
                        for (const target of btnTexts) {
                            if (text.includes(target) && btn.offsetParent !== null) {
                                btn.click();
                                return { clicked: target, source: 'modal' };
                            }
                        }
                    }

                    return { clicked: null, hasModal: false };
                }"""),
                timeout=10
            )

            if result and result.get("clicked"):
                log.info(f"[QwenAI] 已关闭弹窗: {result['clicked']}")
                await asyncio.sleep(1)
                return True

            if result and (result.get("hasModal") or result.get("hasOverlay")):
                await page.keyboard.press('Escape')
                log.info("[QwenAI] 使用 ESC 关闭弹窗")
                await asyncio.sleep(1)
                return True

        except Exception as e:
            log.warning(f"[QwenAI] 检查弹窗异常: {e}")

        return False

    async def _find_input_element(self, page: Any) -> Optional[Any]:
        """查找输入框"""
        selectors = [
            'textarea',
            '[contenteditable="true"]',
            'div[role="textbox"]',
            'input[type="text"]',
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
                if btn and await btn.is_visible():
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
                    if (t && t.length > 20 && !t.includes('qwen') && !t.includes('千问'))
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
            await page.wait_for_selector('textarea', timeout=15000)
        except Exception:
            pass

        # 点击 + 号按钮
        plus_clicked = False
        for sel in ['.mode-select-open', '.ant-dropdown-trigger']:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000, state='visible')
                if btn:
                    await btn.click()
                    plus_clicked = True
                    log.info(f"[QwenAI] 已点击 + 按钮: {sel}")
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        if not plus_clicked:
            log.warning("[QwenAI] 未找到 + 按钮")
            return False

        # 选择图片生成选项
        for sel in ['[data-menu-id*="t2i"]', '.ant-dropdown-menu-item:has-text("生成图像")']:
            try:
                item = await page.wait_for_selector(sel, timeout=5000, state='visible')
                if item:
                    await item.click()
                    log.info(f"[QwenAI] 已选择图片生成模式: {sel}")
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass

        # 兜底：遍历菜单项
        menu_items = await page.query_selector_all('.ant-dropdown-menu-item')
        for item in menu_items:
            try:
                text = (await item.text_content() or "").strip()
                if await item.is_visible() and ('图像' in text or '图片' in text or '生图' in text):
                    await item.click()
                    log.info(f"[QwenAI] 已选择图片生成模式: {text}")
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass

        log.warning("[QwenAI] 未找到图片生成选项")
        return False

    async def _upload_file(self, page: Any, file_path: str) -> bool:
        """上传文件"""
        if not os.path.exists(file_path):
            log.error(f"[QwenAI] 文件不存在: {file_path}")
            return False

        abs_path = os.path.abspath(file_path)
        log.info(f"[QwenAI] 准备上传文件: {abs_path}")

        # 等待页面就绪
        try:
            await page.wait_for_selector('textarea', timeout=15000)
        except Exception:
            pass

        # 点击 + 按钮打开菜单
        plus_clicked = False
        for sel in ['.mode-select-open', '.ant-dropdown-trigger']:
            try:
                btn = await page.wait_for_selector(sel, timeout=5000, state='visible')
                if btn:
                    await btn.click()
                    plus_clicked = True
                    break
            except Exception:
                pass

        if not plus_clicked:
            # 尝试直接设置 file input
            return await self._upload_via_input(page, abs_path)

        # 点击菜单中的 "上传附件"
        await asyncio.sleep(1)
        for sel in ['[data-menu-id*="upload"]', '.ant-dropdown-menu-item:has-text("上传")']:
            try:
                upload_item = await page.wait_for_selector(sel, timeout=5000, state='visible')
                if upload_item:
                    async with page.expect_file_chooser(timeout=5000) as fc_info:
                        await upload_item.click()
                    file_chooser = await fc_info.value
                    await file_chooser.set_files(abs_path)
                    log.info("[QwenAI] 文件上传成功")
                    await asyncio.sleep(2)
                    return True
            except Exception:
                pass

        return False

    async def _upload_via_input(self, page: Any, file_path: str) -> bool:
        """通过 file input 上传文件"""
        try:
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(file_path)
                log.info("[QwenAI] 通过 file input 上传成功")
                await asyncio.sleep(2)
                return True
        except Exception as e:
            log.warning(f"[QwenAI] file input 上传失败: {e}")
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
