"""
匿名浏览器聊天客户端

通过 Camoufox 访问 Qwen 访客模式（/c/guest），
无需登录、无需 token，直接通过页面交互获取 AI 回复。

支持多页签池，并发请求时使用不同页签。
"""
import asyncio
import logging
import os
import time
from typing import AsyncIterator, Optional
from dataclasses import dataclass

log = logging.getLogger("web2api.anonymous")

BASE_URL = "https://chat.qwen.ai"
GUEST_URL = f"{BASE_URL}/c/guest"

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

# 页签池大小
TAB_POOL_SIZE = 5


@dataclass
class AnonymousResponse:
    """匿名聊天响应"""
    content: str
    success: bool = True
    error: str = ""


class QwenAnonymousClient:
    """匿名浏览器聊天客户端 — 单 Camoufox 实例 + 多页签池"""

    def __init__(self, pool_size: int = TAB_POOL_SIZE):
        self._browser = None
        self._camoufox = None
        self._is_ready = False
        self._pool_size = pool_size
        self._tab_pool: list = []  # 固定页签列表
        self._current_idx = 0  # 轮询索引

    # ── 生命周期 ──

    async def _check_alive(self) -> bool:
        """检查浏览器是否存活（evaluate 探测）"""
        if not self._browser:
            return False
        try:
            # 取第一个页签做探测
            if self._tab_pool:
                await asyncio.wait_for(self._tab_pool[0].evaluate("() => true"), timeout=3)
                return True
            return self._browser.is_connected()
        except Exception:
            return False

    async def _ensure_ready(self) -> bool:
        """确保浏览器就绪（复用已有实例或重新启动）"""
        if self._is_ready and await self._check_alive():
            return True
        await self.close()
        return await self.start()

    async def _create_tab(self) -> Optional[object]:
        """创建一个新页签"""
        try:
            if not self._browser or not self._browser.is_connected():
                log.warning("[Anonymous] 浏览器不可用，无法创建页签")
                return None
            page = await self._browser.new_page()
            page.on("pageerror", lambda _: None)
            log.info(f"[Anonymous] 新页签已创建")
            return page
        except Exception as e:
            log.warning(f"[Anonymous] 创建页签失败: {e}")
            return None

    async def _dismiss_popup_on_page(self, page) -> None:
        """在指定页签上关闭弹窗"""
        try:
            await asyncio.wait_for(
                page.evaluate(r"""() => {
                    const overlay = document.querySelector('.qwen-modal-overlay');
                    if (overlay) {
                        const btn = overlay.querySelector('button');
                        if (btn) { btn.click(); return true; }
                    }
                    const btns = document.querySelectorAll('button');
                    const targets = ['Stay logged out', '保持登出', '无需登录', 'Skip', 'Later', 'Close', '关闭'];
                    for (const b of btns) {
                        const t = b.textContent?.trim() || '';
                        for (const target of targets) {
                            if (t.includes(target) && b.offsetParent !== null) {
                                b.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }"""),
                timeout=5
            )
        except Exception:
            pass

    async def _warm_up_pool(self) -> None:
        """串行创建页签池"""
        log.info(f"[Anonymous] 预热页签池 (目标: {self._pool_size})")
        for _ in range(self._pool_size):
            page = await self._create_tab()
            if page:
                try:
                    await page.goto(GUEST_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(0.5)
                    await self._dismiss_popup_on_page(page)
                except Exception as e:
                    log.warning(f"[Anonymous] warm-up tab navigation failed: {e}")
                self._tab_pool.append(page)
        log.info(f"[Anonymous] 页签池预热完成 (可用: {len(self._tab_pool)}/{self._pool_size})")

    async def _acquire_tab(self):
        """轮询获取下一个页签并导航到访客页，页签不可用则重建"""
        if not self._tab_pool:
            log.info("[Anonymous] 页签池为空，创建新页签")
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
            log.warning(f"[Anonymous] 页签 {idx} 已失效，重建")
            page = await self._create_tab()
            if not page:
                return None
            self._tab_pool[idx] = page

        # 导航到访客页（失败时重建该页签）
        try:
            current_url = page.url
            is_landing = current_url.rstrip('/') in (BASE_URL, f"{BASE_URL}/")
            if is_landing or '/c/' not in current_url:
                log.warning(f"[Anonymous] 页面被重定向到 {current_url}，重试导航")
                await page.goto(GUEST_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(0.5)
                current_url = page.url
            await self._dismiss_popup_on_page(page)
            log.info(f"[Anonymous] 页签已就绪: {current_url} (idx={idx})")
            return page
        except Exception as e:
            log.warning(f"[Anonymous] 导航失败 ({e})，重建页签 {idx}")
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass
            new_page = await self._create_tab()
            if new_page:
                self._tab_pool[idx] = new_page
                try:
                    await new_page.goto(GUEST_URL, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(0.5)
                    await self._dismiss_popup_on_page(new_page)
                    log.info(f"[Anonymous] 页签 {idx} 重建成功")
                    return new_page
                except Exception as e2:
                    log.error(f"[Anonymous] 重建后导航仍失败: {e2}")
            return None

    async def start(self, retries: int = 3) -> bool:
        """启动浏览器并预热页签池"""
        for attempt in range(retries):
            try:
                log.info(f"[Anonymous] 启动 Camoufox... (第 {attempt + 1}/{retries} 次)")
                from camoufox.async_api import AsyncCamoufox
                self._camoufox = AsyncCamoufox(**_CAMOUFOX_OPTS)
                self._browser = await self._camoufox.__aenter__()
                await self._warm_up_pool()
                if self._browser and self._browser.is_connected() and self._tab_pool:
                    self._is_ready = True
                    log.info(f"[Anonymous] ✓ 就绪 (页签: {len(self._tab_pool)}/{self._pool_size})")
                    return True
                else:
                    raise Exception("浏览器启动失败")
            except Exception as e:
                log.warning(f"[Anonymous] 启动失败 (第 {attempt + 1}/{retries} 次): {e}")
                await self.close()
                if attempt < retries - 1:
                    await asyncio.sleep(2)
        log.error("[Anonymous] ✗ 所有启动尝试均失败")
        return False

    async def close(self):
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
        self._is_ready = False
        log.info("[Anonymous] 已关闭")

    async def _debug_screenshot(self, name: str) -> None:
        """仅在 DEBUG 级别时截图"""
        if not log.isEnabledFor(logging.DEBUG):
            return
        try:
            debug_path = os.path.join(os.path.dirname(__file__), "..", "..", f"debug_{name}.png")
            await self._page.screenshot(path=debug_path)
            log.debug(f"[Anonymous] 截图: {debug_path}")
        except Exception:
            pass

    # ── 登录弹窗处理 ──

    async def _dismiss_login_popup(self) -> bool:
        """检测并关闭登录弹窗/遮罩层"""
        log.info("[Anonymous] 检查弹窗/遮罩...")
        try:
            result = await asyncio.wait_for(
                self._page.evaluate(r"""() => {
                    // 检查 qwen-modal-overlay
                    const overlay = document.querySelector('.qwen-modal-overlay');
                    if (overlay && overlay.offsetParent !== null) {
                        // 尝试点击遮罩内的按钮
                        const btnTexts = [
                            'Stay logged out', '保持登出', '无需登录', '暂不登录',
                            '不用了', 'Skip', 'Later', 'Close', '关闭', 'Cancel', '取消',
                            'Got it', '知道了', 'OK', '确定',
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
                        // 没找到匹配按钮，点击遮罩本身或第一个按钮
                        const firstBtn = overlay.querySelector('button');
                        if (firstBtn) {
                            firstBtn.click();
                            return { clicked: firstBtn.textContent?.trim(), source: 'overlay-first-btn' };
                        }
                        return { clicked: null, hasOverlay: true };
                    }

                    // 检查其他弹窗
                    const modals = document.querySelectorAll('[role="dialog"], [class*="modal"]:not(.qwen-modal-overlay)');
                    let hasModal = false;
                    for (const m of modals) {
                        if (m.offsetParent !== null) {
                            hasModal = true;
                            break;
                        }
                    }

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

                    return { clicked: null, hasModal };
                }"""),
                timeout=10
            )

            if result and result.get("clicked"):
                log.info(f"[Anonymous] 已关闭弹窗: {result['clicked']} (来源: {result.get('source')})")
                await asyncio.sleep(1)
                return True

            if result and (result.get("hasModal") or result.get("hasOverlay")):
                await self._page.keyboard.press('Escape')
                log.info("[Anonymous] 使用 ESC 关闭弹窗/遮罩")
                await asyncio.sleep(1)
                return True

        except asyncio.TimeoutError:
            log.warning("[Anonymous] 检查弹窗超时")
        except Exception as e:
            log.warning(f"[Anonymous] 检查弹窗异常: {e}")

        log.info("[Anonymous] 未检测到弹窗，继续")
        return False

    # ── 模式切换 ──

    async def _select_image_mode(self, image_options: dict | None = None) -> bool:
        """切换到图片生成模式（点击 + 号 → 选择图片生成）

        Args:
            image_options: 图片选项，包含比例等信息
        """
        # 等待页面就绪
        try:
            await self._page.wait_for_selector('textarea', timeout=15000)
        except Exception:
            pass

        # 1. 点击 + 号按钮（优先使用精确选择器）
        plus_clicked = False
        for sel in ['.mode-select-open', '.mode-select-open-active', '.ant-dropdown-trigger']:
            try:
                btn = await self._page.wait_for_selector(sel, timeout=5000, state='visible')
                if btn:
                    await btn.click()
                    plus_clicked = True
                    log.info(f"[Anonymous] 已点击 + 按钮: {sel}")
                    await asyncio.sleep(1)
                    break
            except Exception:
                pass

        # 兜底：通用 + 号选择器
        if not plus_clicked:
            plus_selectors = [
                'button[aria-label*="plus" i]',
                'button[aria-label*="添加" i]',
                'button[aria-label*="more" i]',
                'button[aria-label*="更多" i]',
                'button:has-text("+")',
                '[class*="plus"]',
                '[class*="add-btn"]',
                '[class*="more-action"]',
                'div[role="button"]:has-text("+")',
                'textarea + button',
                '[contenteditable] + button',
                '[contenteditable] ~ button',
            ]
            for sel in plus_selectors:
                try:
                    btn = await self._page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        plus_clicked = True
                        log.info(f"[Anonymous] 已点击 + 号: {sel}")
                        await asyncio.sleep(1.5)
                        break
                except Exception:
                    pass

        if not plus_clicked:
            log.warning("[Anonymous] 未找到 + 号按钮")
            return False

        # 2. 选择图片生成选项（优先使用精确选择器）
        # 优先尝试 data-menu-id
        mode_switched = False
        for sel in ['[data-menu-id*="t2i"]', '.ant-dropdown-menu-item:has-text("生成图像")']:
            try:
                item = await self._page.wait_for_selector(sel, timeout=5000, state='visible')
                if item:
                    await item.click()
                    log.info(f"[Anonymous] 已选择图片生成模式: {sel}")
                    mode_switched = True
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass

        if not mode_switched:
            # 兜底：遍历菜单项
            await asyncio.sleep(1)
            menu_items = await self._page.query_selector_all('.ant-dropdown-menu-item')
            for item in menu_items:
                try:
                    text = (await item.text_content() or "").strip()
                    if await item.is_visible() and ('图像' in text or '图片' in text or '生图' in text):
                        await item.click()
                        log.info(f"[Anonymous] 已选择图片生成模式（文本匹配）: {text}")
                        mode_switched = True
                        await asyncio.sleep(2)
                        break
                except Exception:
                    pass

        if not mode_switched:
            # 最终兜底：通用选择器
            img_selectors = [
                'button:has-text("图片生成")',
                'button:has-text("图片")',
                'button:has-text("图像生成")',
                'button:has-text("Image")',
                'button:has-text("image")',
                'button:has-text("生图")',
                'button:has-text("AI 画图")',
                'button:has-text("画图")',
                'div[role="button"]:has-text("图片生成")',
                'div[role="button"]:has-text("图片")',
                'li:has-text("图片生成")',
                'li:has-text("图片")',
                '[class*="menu"] button:has-text("图")',
                '[class*="menu"] div:has-text("图片生成")',
                '[class*="menu-item"]:has-text("图")',
            ]

            for sel in img_selectors:
                try:
                    btn = await self._page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        log.info(f"[Anonymous] 已选择图片生成: {sel}")
                        mode_switched = True
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

        if not mode_switched:
            log.warning("[Anonymous] 未找到图片生成选项")
            await self._debug_screenshot("no_img_mode")
            return False

        # 3. 设置图片比例（如果提供了参数）
        aspect_ratio = image_options.get("ratio") if image_options else None
        log.info(f"[Anonymous] 检查图片比例参数: aspect_ratio={aspect_ratio}")
        if aspect_ratio:
            await self._set_aspect_ratio(aspect_ratio)
        else:
            log.info("[Anonymous] 未提供图片比例参数，使用默认比例")

        return True

    async def _set_aspect_ratio(self, aspect_ratio: str) -> bool:
        """设置图片比例

        Args:
            aspect_ratio: 图片比例，如 "16:9", "1:1", "9:16", "3:4", "4:3" 等
        """
        log.info(f"[Anonymous] 设置图片比例: {aspect_ratio}")
        try:
            # 点击比例选择器打开下拉菜单
            selector = await self._page.query_selector('.size-selector .ant-dropdown-trigger, .size-selector-popup')
            if not selector:
                # 尝试其他选择器
                selector = await self._page.query_selector('.selector-text')

            if selector and await selector.is_visible():
                await selector.click()
                await asyncio.sleep(1)
                log.info("[Anonymous] 已点击比例选择器")
            else:
                log.warning("[Anonymous] 未找到比例选择器")
                return False

            # 等待下拉菜单出现
            await asyncio.sleep(0.5)

            # 查找并点击目标比例（使用 data-menu-id 或文本匹配）
            menu_items = await self._page.query_selector_all('.ant-dropdown-menu-item')
            for item in menu_items:
                try:
                    # 获取菜单项文本
                    title_el = await item.query_selector('.ant-dropdown-menu-title-content')
                    if title_el:
                        text = (await title_el.text_content() or "").strip()
                        # 清理文本（移除额外的图标等）
                        text = text.replace('\n', '').strip()
                        if text == aspect_ratio:
                            await item.click()
                            log.info(f"[Anonymous] 已选择图片比例: {aspect_ratio}")
                            await asyncio.sleep(0.5)
                            return True
                except Exception:
                    pass

            # 兜底：直接遍历所有菜单项的文本
            for item in menu_items:
                try:
                    text = (await item.text_content() or "").strip()
                    if aspect_ratio in text:
                        await item.click()
                        log.info(f"[Anonymous] 已选择图片比例（文本包含）: {aspect_ratio}")
                        await asyncio.sleep(0.5)
                        return True
                except Exception:
                    pass

            log.warning(f"[Anonymous] 未找到图片比例选项: {aspect_ratio}")
            # 关闭下拉菜单
            await self._page.keyboard.press('Escape')
            return False

        except Exception as e:
            log.error(f"[Anonymous] 设置图片比例失败: {e}")
            return False

    # ── 文件上传 ──

    async def _upload_file(self, file_path: str) -> bool:
        """通过 + 菜单上传文件

        Args:
            file_path: 要上传的文件路径
        """
        if not os.path.exists(file_path):
            log.error(f"[Anonymous] 文件不存在: {file_path}")
            return False

        abs_path = os.path.abspath(file_path)
        log.info(f"[Anonymous] 准备上传文件: {abs_path}")

        # 等待页面就绪
        try:
            await self._page.wait_for_selector('textarea', timeout=15000)
        except Exception:
            pass

        # 步骤 A：点击 + 按钮打开菜单
        plus_clicked = False
        for sel in ['.mode-select-open', '.mode-select-open-active', '.ant-dropdown-trigger']:
            try:
                btn = await self._page.wait_for_selector(sel, timeout=5000, state='visible')
                if btn:
                    await btn.click()
                    plus_clicked = True
                    log.info(f"[Anonymous] 已点击 + 按钮: {sel}")
                    break
            except Exception:
                pass

        if not plus_clicked:
            # 兜底：尝试其他 + 号选择器
            plus_selectors = [
                'button[aria-label*="plus" i]',
                'button[aria-label*="添加" i]',
                'button[aria-label*="more" i]',
                'button:has-text("+")',
                '[class*="plus"]',
                '[class*="add-btn"]',
            ]
            for sel in plus_selectors:
                try:
                    btn = await self._page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        plus_clicked = True
                        log.info(f"[Anonymous] 已点击 + 号: {sel}")
                        await asyncio.sleep(1)
                        break
                except Exception:
                    pass

        if not plus_clicked:
            log.warning("[Anonymous] 未找到 + 按钮，尝试直接设置 file input...")
            return await self._upload_via_input(abs_path)

        # 步骤 B：点击菜单中的 "上传附件"
        await asyncio.sleep(1)
        upload_item = None

        for sel in ['[data-menu-id*="upload"]', '.ant-dropdown-menu-item:has-text("上传")']:
            try:
                upload_item = await self._page.wait_for_selector(sel, timeout=5000, state='visible')
                if upload_item:
                    log.info(f"[Anonymous] 找到上传菜单项: {sel}")
                    break
            except Exception:
                pass

        # 兜底：遍历菜单项
        if not upload_item:
            items = await self._page.query_selector_all('.ant-dropdown-menu-item')
            for item in items:
                try:
                    text = (await item.text_content() or "").strip()
                    if await item.is_visible() and '上传' in text:
                        upload_item = item
                        log.info(f"[Anonymous] 找到上传菜单项（文本匹配）: {text}")
                        break
                except Exception:
                    pass

        # 步骤 C：拦截文件选择对话框并上传
        if upload_item:
            try:
                async with self._page.expect_file_chooser(timeout=5000) as fc_info:
                    await upload_item.click()
                file_chooser = await fc_info.value
                await file_chooser.set_files(abs_path)
                log.info("[Anonymous] 文件上传成功!")
                return True
            except Exception as e:
                log.warning(f"[Anonymous] 文件选择对话框拦截失败: {e}")

        # 兜底：直接设置 file input
        return await self._upload_via_input(abs_path)

    async def _upload_via_input(self, file_path: str) -> bool:
        """通过直接设置 file input 上传文件"""
        try:
            upload_input = await self._page.wait_for_selector('#filesUpload, input[type="file"]', timeout=5000)
            if upload_input:
                await upload_input.set_input_files(file_path)
                log.info("[Anonymous] 通过 file input 上传文件成功")
                return True
        except Exception as e:
            log.warning(f"[Anonymous] file input 上传失败: {e}")

        # 最终兜底：JavaScript 注入
        try:
            result = await self._page.evaluate("""(filePath) => {
                const input = document.querySelector('#filesUpload, input[type="file"]');
                if (!input) return false;
                // 创建 DataTransfer 模拟文件
                return true;
            }""", file_path)
            if result:
                log.info("[Anonymous] 通过 JS 注入上传")
                return True
        except Exception:
            pass

        log.error("[Anonymous] 所有上传方式均失败")
        await self._debug_screenshot("upload_fail")
        return False

    async def _verify_upload(self) -> bool:
        """验证文件是否上传成功（带重试）"""
        for attempt in range(10):
            await asyncio.sleep(2 if attempt == 0 else 1.5)
            upload_status = await self._page.evaluate(r"""() => {
                const result = { files: [], hasFileCard: false };

                // 检测 file-card-list 容器内的 vision-item-container（实际 DOM 结构）
                const fileCardList = document.querySelector('.file-card-list');
                if (fileCardList) {
                    const items = fileCardList.querySelectorAll('.vision-item-container');
                    result.hasFileCard = items.length > 0;
                    items.forEach(item => {
                        const img = item.querySelector('.vision-item-image');
                        result.files.push({ name: img ? img.alt : '' });
                    });
                }

                // 兜底：直接检测 vision-item-container
                if (!result.hasFileCard) {
                    const altItems = document.querySelectorAll('.vision-item-container');
                    result.hasFileCard = altItems.length > 0;
                    altItems.forEach(item => {
                        const img = item.querySelector('.vision-item-image, img');
                        if (img) result.files.push({ name: img.alt || '' });
                    });
                }

                return result;
            }""")

            if upload_status['hasFileCard']:
                log.info(f"[Anonymous] 检测到 {len(upload_status['files'])} 个已上传文件:")
                for f in upload_status['files']:
                    log.info(f"  - {f['name']}")
                return True

            log.info(f"[Anonymous] 等待文件处理... (attempt={attempt + 1}/10)")

        log.warning("[Anonymous] 上传验证超时，继续发送")
        return True  # 不阻塞流程，允许继续发送

    # ── 输入与发送 ──

    async def _find_input_element(self, retries: int = 2):
        """查找输入框（带重试）"""
        selectors = [
            'textarea',
            '[contenteditable="true"]',
            'div[role="textbox"]',
            'input[type="text"]',
        ]
        for attempt in range(retries):
            for sel in selectors:
                try:
                    elements = await self._page.query_selector_all(sel)
                    for el in elements:
                        if await el.is_visible():
                            return el, sel
                except Exception:
                    pass
            if attempt < retries - 1:
                log.info(f"[Anonymous] 未找到输入框，等待重试 ({attempt + 1}/{retries})")
                await asyncio.sleep(0.5)
        return None, None

    async def _type_message(self, message: str) -> bool:
        """输入消息"""
        el, sel = await self._find_input_element()
        if not el:
            # 截图帮助诊断
            await self._debug_screenshot("no_input")
            try:
                text = await self._page.evaluate("() => document.body.innerText")
                log.error(f"[Anonymous] 找不到输入框，页面文本 (前 500 字): {text[:500]}")
            except Exception:
                log.error("[Anonymous] 找不到输入框")
            return False

        try:
            # 聚焦输入框
            await el.click()
            await asyncio.sleep(0.1)

            # 清空输入框
            await self._page.keyboard.press('Control+A')
            await self._page.keyboard.press('Delete')
            await asyncio.sleep(0.05)

            # 用 JS native setter 输入（避免 DOM 引用失效 + React 兼容）
            inserted = await self._page.evaluate(r"""(message) => {
                const el = document.querySelector('textarea') ||
                           document.querySelector('[contenteditable="true"]') ||
                           document.querySelector('div[role="textbox"]');
                if (!el) return false;
                el.focus();
                // native setter 设置值（绕过 React 受控组件的 value 拦截）
                const nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLTextAreaElement.prototype, 'value'
                )?.set;
                if (nativeSet) {
                    nativeSet.call(el, message);
                    // 触发 React 追踪器（react internalInstanceTracker）
                    const tracker = el._valueTracker;
                    if (tracker) tracker.setValue('');  // 让 React 认为值变了
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                } else {
                    // contenteditable fallback
                    el.textContent = message;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
                return true;
            }""", message)

            if not inserted:
                # JS 注入失败，用 keyboard.insertText 兜底（不触发逐字事件）
                await self._page.keyboard.insertText(message)
                log.info(f"[Anonymous] 使用 insertText 输入消息 ({len(message)} 字)")
            else:
                log.info(f"[Anonymous] 使用 JS native setter 输入消息 ({len(message)} 字)")

            # 验证输入框内容
            input_value = await self._page.evaluate("""() => {
                const el = document.querySelector('textarea');
                if (el) return el.value;
                const ce = document.querySelector('[contenteditable="true"]');
                if (ce) return ce.textContent || ce.innerText || '';
                return '';
            }""")
            log.info(f"[Anonymous] 输入框内容: {input_value[:100]}...")
            await self._debug_screenshot("after_type")

            return True
        except Exception as e:
            log.error(f"[Anonymous] 输入失败: {e}")
            return False

    async def _send_message(self) -> bool:
        """发送消息"""
        log.info("[Anonymous] 开始发送消息...")

        # 等待 React 处理输入状态
        await asyncio.sleep(0.3)

        # 优先用 Enter 键发送（对 React 受控输入框更可靠）
        # 先确认输入框有内容
        has_text = await self._page.evaluate(r"""() => {
            const ta = document.querySelector('textarea');
            return ta ? ta.value.trim().length > 0 : false;
        }""")
        if has_text:
            await self._page.keyboard.press('Enter')
            log.info("[Anonymous] Enter 键发送")
            await self._after_send()
            return True

        # Enter 不可用时，尝试点击发送按钮
        send_selectors = [
            '.message-input-right-button-send',  # Qwen 特定的发送按钮
            '.omni-button-content-btn',          # Omni 按钮
            'button[type="submit"]',
            'button[aria-label*="send" i]',
            'button[aria-label*="Send" i]',
            'button[aria-label*="发送"]',
        ]
        for sel in send_selectors:
            try:
                btn = await self._page.query_selector(sel)
                if btn and await btn.is_visible() and not await btn.is_disabled():
                    await btn.click()
                    log.info(f"[Anonymous] 点击发送按钮: {sel}")
                    await self._after_send()
                    return True
            except Exception:
                pass

        # 最终兜底
        log.warning("[Anonymous] 未找到可用的发送方式，尝试 Enter")
        await self._page.keyboard.press('Enter')
        await self._after_send()
        return True

    async def _after_send(self):
        """发送后处理：等待响应 + 检测页面跳转"""
        url_before = self._page.url
        log.info(f"[Anonymous] 发送前 URL: {url_before}")

        # 等待 AI 开始响应或页面跳转
        try:
            await self._page.wait_for_selector(
                'button[aria-label="Stop"], button[aria-label="停止"], [class*="message-content"]',
                timeout=5000
            )
        except Exception:
            pass

        await asyncio.sleep(0.5)
        url_after = self._page.url
        log.info(f"[Anonymous] 发送后 URL: {url_after}")

        # If the page stays on new-chat, keep waiting; a second Enter can duplicate the request.
        if url_after == url_before and 'new-chat' in url_after:
            log.info("[Anonymous] URL unchanged after send; waiting for response without resubmitting")
            return

        # 检查同上下文下的其他页面
        try:
            for p in self._page.context.pages:
                if p != self._page:
                    log.info(f"[Anonymous] 发现其他页面: {p.url}")
        except Exception:
            pass

        if url_after != url_before:
            log.info(f"[Anonymous] 页面已跳转: {url_before} → {url_after}")
            try:
                await self._page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass

    async def _wait_after_send(self):
        """发送后等待 AI 开始响应（替代硬等待）"""
        try:
            await self._page.wait_for_selector(
                'button[aria-label="Stop"], button[aria-label="停止"], [class*="message-content"]',
                timeout=5000
            )
        except Exception:
            pass  # 响应可能非常快

    # ── 回复获取 ──

    async def _get_reply_text(self) -> Optional[str]:
        """从页面获取最新 AI 回复文本

        DOM 结构:
        - 用户消息: .user-message-content (p 标签)
        - AI 回复: .response-message-content > .qwen-markdown > .qwen-markdown-text (span 标签)

        取最后一个 .response-message-content 内的文本。
        """
        if not self._page:
            return None
        result = await self._page.evaluate(r"""() => {
            // Qwen UI 元素文本，需要过滤
            const UI_MARKERS = [
                '已经完成思考',
                'Thinking completed',
                'Thinking...',
                'AI-generated content may not be accurate',
            ];

            function cleanText(text) {
                if (!text) return text;
                let cleaned = text;
                for (const marker of UI_MARKERS) {
                    cleaned = cleaned.replace(marker, '');
                }
                return cleaned.trim();
            }

            // 图片提取
            function extractImages(el) {
                let parts = [];
                const isPlaceholder = (src) => {
                    if (!src) return true;
                    if (src.match(/\.(png|jpg|jpeg|webp)\?key=/i)) return false;
                    if (src.includes('cdn.qwenlm.ai') && !src.match(/\.(png|jpg|jpeg|webp)/i)) return true;
                    return false;
                };
                el.querySelectorAll('img[src]').forEach(img => {
                    const src = img.src;
                    if (src && src.startsWith('http') && !isPlaceholder(src)) {
                        parts.push('![image](' + src + ')');
                    }
                });
                return parts;
            }

            // 策略 1: 取最后一个 .response-message-content 内的文本
            const responseContainers = document.querySelectorAll('.response-message-content');
            if (responseContainers.length > 0) {
                const lastContainer = responseContainers[responseContainers.length - 1];
                // 收集所有 .qwen-markdown-text span 的文本
                const textSpans = lastContainer.querySelectorAll('.qwen-markdown-text');
                let textParts = [];
                textSpans.forEach(span => {
                    const t = span.textContent?.trim();
                    if (t) textParts.push(t);
                });
                let text = cleanText(textParts.join(''));
                // 提取图片
                const imgs = extractImages(lastContainer);
                if (imgs.length > 0) text += '\n' + imgs.join('\n');
                if (text && text.length > 3) {
                    return {text: text.substring(0, 16000), method: 'response-message-content'};
                }
            }

            // 策略 2: 取最后一个 .qwen-markdown-text span（全局）
            const allSpans = document.querySelectorAll('.qwen-markdown-text');
            if (allSpans.length > 0) {
                // 从后往前找，跳过用户消息容器内的
                for (let i = allSpans.length - 1; i >= 0; i--) {
                    const span = allSpans[i];
                    // 检查是否在用户消息容器内
                    const parent = span.closest('.user-message-content, .chat-user-message');
                    if (parent) continue; // 跳过用户消息
                    const t = cleanText(span.textContent?.trim());
                    if (t && t.length > 3) {
                        return {text: t.substring(0, 16000), method: 'qwen-markdown-text:last'};
                    }
                }
            }

            return {text: null, method: 'none'};
        }""")

        text = result.get("text") if result else None
        method = result.get("method") if result else "unknown"
        if text:
            log.info(f"[Anonymous] _get_reply_text 提取成功 ({len(text)}字, 方法={method})")
        else:
            log.debug(f"[Anonymous] _get_reply_text 未找到内容 (方法={method})")
        return text

    async def _is_generating(self) -> bool:
        """检查是否正在生成中"""
        try:
            return await asyncio.wait_for(
                self._page.evaluate("""() => {
                    const stop = document.querySelector('button[aria-label="Stop"], button[aria-label="停止"]');
                    const loading = document.querySelector('[class*="loading"], [class*="generating"], [class*="spinner"]');
                    return !!(stop || loading);
                }"""),
                timeout=10
            )
        except asyncio.TimeoutError:
            log.warning("[Anonymous] _is_generating 超时")
            return False

    async def _poll_image_generation(self) -> dict:
        """轮询图片生成状态（单次 evaluate 获取所有信息）"""
        try:
            return await asyncio.wait_for(
                self._page.evaluate(r"""() => {
            const result = { isGenerating: false, images: [], debug: {} };

            // 生成状态
            const stop = document.querySelector('button[aria-label="Stop"], button[aria-label="停止"]');
            const loading = document.querySelector('[class*="loading"], [class*="generating"], [class*="spinner"]');
            result.isGenerating = !!(stop || loading);

            // 占位图过滤：无文件后缀且无 ?key= 参数的是占位图
            const isPlaceholder = (src) => {
                if (!src) return true;
                if (src.match(/\.(png|jpg|jpeg|webp)\?key=/i)) return false;
                if (src.includes('cdn.qwenlm.ai') && !src.match(/\.(png|jpg|jpeg|webp)/i)) return true;
                return false;
            };

            const seen = new Set();

            // 1. .qwen-markdown-image-content 容器内的 img
            document.querySelectorAll('.qwen-markdown-image-content').forEach(container => {
                const img = container.querySelector('img');
                if (img && img.src && !isPlaceholder(img.src) && !seen.has(img.src)) {
                    seen.add(img.src);
                    result.images.push({ src: img.src, w: img.naturalWidth || 0, h: img.naturalHeight || 0 });
                }
            });

            // 2. .ant-image 容器内的 img
            document.querySelectorAll('.ant-image').forEach(container => {
                const img = container.querySelector('img');
                if (img && img.src && !isPlaceholder(img.src) && !seen.has(img.src)) {
                    seen.add(img.src);
                    result.images.push({ src: img.src, w: img.naturalWidth || 0, h: img.naturalHeight || 0 });
                }
            });

            // 3. 兜底：所有 img.qwen-image / img.ant-image-img
            if (result.images.length === 0) {
                document.querySelectorAll('img.qwen-image, img.ant-image-img').forEach(img => {
                    if (img.src && !isPlaceholder(img.src) && !seen.has(img.src)) {
                        seen.add(img.src);
                        result.images.push({ src: img.src, w: img.naturalWidth || 0, h: img.naturalHeight || 0 });
                    }
                });
            }

            // 4. 最终兜底：cdn.qwenlm.ai 上的真实图片
            if (result.images.length === 0) {
                document.querySelectorAll('img[src*="cdn.qwenlm.ai"]').forEach(img => {
                    if (img.src && !isPlaceholder(img.src) && !seen.has(img.src)) {
                        seen.add(img.src);
                        result.images.push({ src: img.src, w: img.naturalWidth || 0, h: img.naturalHeight || 0 });
                    }
                });
            }

            // 调试统计
            const allImgs = document.querySelectorAll('img');
            let ph = 0, cdn = 0, oth = 0;
            allImgs.forEach(img => {
                if (!img.src) return;
                if (img.src.includes('/image_gen/')) ph++;
                else if (img.src.includes('cdn.qwenlm.ai')) cdn++;
                else oth++;
            });
            result.debug = { total: allImgs.length, ph, cdn, oth };
            return result;
        }"""),
                timeout=15
            )
        except asyncio.TimeoutError:
            log.warning("[Anonymous] _poll_image_generation 超时")
            return {"isGenerating": False, "images": [], "debug": {"total": 0, "ph": 0, "cdn": 0}}
        except Exception as e:
            log.error(f"[Anonymous] _poll_image_generation 异常: {e}")
            return {"isGenerating": False, "images": [], "debug": {"total": 0, "ph": 0, "cdn": 0}}

    # ── MutationObserver 真流式 ──

    async def _setup_reply_observer(self) -> None:
        """注入 MutationObserver 监听最后一个回复容器的 DOM 变化"""
        await self._page.evaluate(r"""() => {
            // 清理旧 observer
            if (window.__qwen_observer) { window.__qwen_observer.disconnect(); }
            window.__qwen_reply = { text: '', changed: false, stableSince: 0, found: false };

            // UI 标记过滤
            const UI_MARKERS = [
                '已经完成思考', 'Thinking completed', 'Thinking...',
                'AI-generated content may not be accurate',
            ];

            function cleanText(text) {
                if (!text) return text;
                let cleaned = text;
                for (const marker of UI_MARKERS) {
                    cleaned = cleaned.replace(marker, '');
                }
                return cleaned.trim();
            }

            // 提取回复文本（只取 .qwen-markdown-text span 的文本）
            function extractReplyText(container) {
                const textSpans = container.querySelectorAll('.qwen-markdown-text');
                let parts = [];
                textSpans.forEach(span => {
                    const t = span.textContent?.trim();
                    if (t) parts.push(t);
                });
                let text = parts.join('');
                // 提取图片
                const imgs = [];
                container.querySelectorAll('img[src]').forEach(img => {
                    const src = img.src;
                    if (src && src.startsWith('http') && !src.match(/\.(png|jpg|jpeg|webp)\?key=/i)) {
                        imgs.push('![image](' + src + ')');
                    }
                });
                if (imgs.length > 0) text += '\n' + imgs.join('\n');
                return cleanText(text);
            }

            // 取最后一个 .response-message-content 容器
            const containers = document.querySelectorAll('.response-message-content');
            const target = containers.length > 0 ? containers[containers.length - 1] : null;

            if (!target) return;

            window.__qwen_reply.found = true;
            window.__qwen_reply.text = extractReplyText(target);
            window.__qwen_reply.stableSince = Date.now();

            window.__qwen_observer = new MutationObserver(() => {
                window.__qwen_reply.text = extractReplyText(target);
                window.__qwen_reply.changed = true;
                window.__qwen_reply.stableSince = Date.now();
            });
            window.__qwen_observer.observe(target, { childList: true, subtree: true, characterData: true });
        }""")

    async def _poll_reply_state(self) -> dict:
        """单次 evaluate 获取 observer 状态 + 生成状态"""
        try:
            return await asyncio.wait_for(
                self._page.evaluate(r"""() => {
                    const r = window.__qwen_reply || { text: '', changed: false, stableSince: 0, found: false };
                    const stop = document.querySelector('button[aria-label="Stop"], button[aria-label="停止"]');
                    const loading = document.querySelector('[class*="loading"], [class*="generating"], [class*="spinner"]');
                    return {
                        text: r.text || '',
                        found: r.found,
                        generating: !!(stop || loading),
                        stableSince: r.stableSince || 0,
                    };
                }"""),
                timeout=10
            )
        except Exception:
            return {"text": "", "found": False, "generating": False, "stableSince": 0}

    async def _wait_reply_stream(self, timeout_sec: int = 120, user_message: str = "", mode: str | None = None) -> AsyncIterator[dict]:
        """流式等待回复 — yield {"content": delta} / {"done": True}

        文本模式：使用 MutationObserver 监听 DOM 变化，实现真正的流式返回。
        图片模式：使用 _poll_image_generation 轮询图片生成状态。
        """
        start = time.time()
        last_content = ""
        stable_count = 0
        observer_setup = False
        no_progress_count = 0
        log.info(f"[Anonymous] 开始流式等待 (超时: {timeout_sec}s, 模式: {mode})")

        # 等待 AI 开始响应
        await asyncio.sleep(1)
        current_url = self._page.url
        log.info(f"[Anonymous] 当前 URL: {current_url}")

        # 如果跳转到着陆页，说明发送失败
        is_landing = current_url.rstrip('/') in ('https://chat.qwen.ai', 'https://chat.qwen.ai/') or '/c/' not in current_url
        if is_landing:
            log.warning(f"[Anonymous] page returned to landing after send: {current_url}; stop to avoid duplicate submit")
            yield {"error": f"page returned to landing after send: {current_url}"}
            return

        # ── 图片生成模式：轮询图片 URL ──
        if mode == "image":
            log.info(f"[Anonymous] 图片生成模式，使用轮询等待")
            while time.time() - start < timeout_sec:
                if not self._page:
                    yield {"error": "页签已释放"}
                    return

                elapsed = time.time() - start

                # 定期检查登录弹窗
                if int(elapsed) % 10 == 0:
                    try:
                        await self._dismiss_login_popup()
                    except Exception:
                        pass

                try:
                    poll_result = await self._poll_image_generation()
                    is_generating = poll_result["isGenerating"]
                    images = poll_result["images"]
                    debug = poll_result["debug"]

                    log.debug(f"[Anonymous] [{elapsed:.0f}s] gen={is_generating} found={len(images)} | total={debug['total']} ph={debug['ph']} cdn={debug['cdn']}")
                except Exception as e:
                    log.error(f"[Anonymous] [{elapsed:.0f}s] 轮询图片生成状态异常: {e}")
                    await asyncio.sleep(2)
                    continue

                if images:
                    log.info(f"[Anonymous] [{elapsed:.0f}s] 检测到 {len(images)} 张图片")
                    image_urls = [f"![image]({img['src']})" for img in images]
                    content = "\n".join(image_urls)
                    yield {"content": content}
                    yield {"done": True, "final_content": content, "reason": "image_found"}
                    return

                if is_generating:
                    no_progress_count = 0
                else:
                    no_progress_count += 1
                    if no_progress_count > 10:
                        error_text = await self._page.evaluate("""() => {
                            const errors = document.querySelectorAll('[class*="error"], [class*="fail"]');
                            for (const el of errors) { const t = el.textContent?.trim(); if (t && t.length > 5) return t.substring(0, 200); }
                            return null;
                        }""")
                        if error_text:
                            log.warning(f"[Anonymous] 检测到错误: {error_text}")

                await asyncio.sleep(2)

            log.warning(f"[Anonymous] 图片生成超时 ({timeout_sec}s)")
            if last_content:
                yield {"done": True, "final_content": last_content, "reason": "image_timeout_with_content"}
            else:
                yield {"error": "回复超时或为空"}
            return

        # ── 文本模式：MutationObserver 真流式 ──
        while time.time() - start < timeout_sec:
            # 检查页签是否仍然有效
            if not self._page:
                log.warning("[Anonymous] 页签已释放，停止流式等待")
                if last_content:
                    yield {"done": True, "final_content": last_content, "reason": "page_released_with_content"}
                else:
                    yield {"error": "页签已释放"}
                return

            # 检测到回复内容时立即设置 MutationObserver
            if not observer_setup:
                # 先用 _get_reply_text 检测是否有内容
                content = await self._get_reply_text()
                if content:
                    # 有内容了，立即设置 observer
                    try:
                        await self._setup_reply_observer()
                        observer_setup = True
                        log.info("[Anonymous] MutationObserver 已设置")
                        # 使用 observer 的文本（更精确）
                        state = await self._poll_reply_state()
                        observer_text = state.get("text", "")
                        if observer_text:
                            content = observer_text
                    except Exception as e:
                        log.debug(f"[Anonymous] 设置 observer 失败: {e}")

                if content and user_message and content.strip() == user_message.strip():
                    content = None

                if content and len(content) > len(last_content):
                    delta = content[len(last_content):]
                    if delta:
                        log.info(f"[Anonymous] 流式 delta={len(delta)}字 (轮询)")
                        yield {"content": delta}
                    last_content = content
                    stable_count = 0
                elif content and content == last_content:
                    stable_count += 1
                    if stable_count >= 3:
                        log.info(f"[Anonymous] 流式完成 (轮询稳定)")
                        yield {"done": True, "final_content": last_content, "reason": "poll_stable"}
                        return

                await asyncio.sleep(0.1)
                continue

            # 使用 MutationObserver 获取增量内容
            else:
                try:
                    state = await self._poll_reply_state()
                    content = state.get("text", "")
                    generating = state.get("generating", False)

                    # 过滤用户消息（精确匹配）
                    if content and user_message and content.strip() == user_message.strip():
                        content = ""

                    # 清理 UI 标记
                    if content:
                        UI_MARKERS = [
                            '已经完成思考', 'Thinking completed', 'Thinking...',
                            'AI-generated content may not be accurate',
                        ]
                        for marker in UI_MARKERS:
                            content = content.replace(marker, '')
                        content = content.strip()

                    # 有新内容
                    if content and len(content) > len(last_content):
                        delta = content[len(last_content):]
                        if delta:
                            log.info(f"[Anonymous] 流式 delta={len(delta)}字")
                            yield {"content": delta}
                        last_content = content
                        stable_count = 0
                    elif content and content == last_content:
                        # 内容稳定
                        stable_count += 1
                        # 生成结束且内容稳定，判定完成
                        if not generating and stable_count >= 3:
                            log.info(f"[Anonymous] 流式完成 (生成结束，内容稳定)")
                            yield {"done": True, "final_content": last_content, "reason": "generation_stopped"}
                            return
                        # 生成中但内容长时间不变，可能是思考阶段
                        if generating and stable_count >= 20:
                            log.info(f"[Anonymous] 流式完成 (内容长时间稳定)")
                            yield {"done": True, "final_content": last_content, "reason": "content_stable_while_generating"}
                            return
                    # else: 没内容或内容变短，继续等
                except Exception as e:
                    log.debug(f"[Anonymous] 轮询 observer 状态失败: {e}")
                    # fallback 到 _get_reply_text
                    content = await self._get_reply_text()
                    if content and user_message and content.strip() == user_message.strip():
                        content = None
                    if content and content != last_content:
                        delta = content[len(last_content):]
                        if delta:
                            yield {"content": delta}
                        last_content = content
                        stable_count = 0
                    elif content and content == last_content:
                        stable_count += 1
                        if stable_count >= 3:
                            yield {"done": True, "final_content": last_content, "reason": "fallback_stable"}
                            return

            await asyncio.sleep(0.1)

        log.warning(f"[Anonymous] 流式超时 ({timeout_sec}s)")
        if last_content:
            yield {"done": True, "final_content": last_content, "reason": "timeout_with_content"}
        else:
            yield {"error": "回复超时或为空"}

    async def _wait_reply(self, timeout_sec: int = 120, user_message: str = "", mode: str | None = None) -> Optional[str]:
        """等待回复完成，返回最终文本

        Args:
            timeout_sec: 超时时间（秒）
            user_message: 用户发送的消息（用于过滤）
            mode: "image" 表示图片生成模式
        """
        start = time.time()
        last_content = ""
        stable_count = 0
        no_progress_count = 0
        log.info(f"[Anonymous] 开始等待回复 (超时: {timeout_sec}s, 模式: {mode})")

        while time.time() - start < timeout_sec:
            await asyncio.sleep(3)
            elapsed = time.time() - start

            # 检查登录弹窗（每 10 秒检查一次）
            if int(elapsed) % 10 == 0:
                await self._dismiss_login_popup()

            if mode == "image":
                # 图片生成模式：使用单次 evaluate 获取所有信息
                try:
                    log.info(f"[Anonymous] [{elapsed:.0f}s] 开始轮询图片生成状态...")
                    poll_result = await self._poll_image_generation()
                    is_generating = poll_result["isGenerating"]
                    images = poll_result["images"]
                    debug = poll_result["debug"]

                    log.info(f"[Anonymous] [{elapsed:.0f}s] gen={is_generating} found={len(images)} | total={debug['total']} ph={debug['ph']} cdn={debug['cdn']}")
                except Exception as e:
                    log.error(f"[Anonymous] [{elapsed:.0f}s] 轮询图片生成状态异常: {e}")
                    import traceback
                    traceback.print_exc()
                    continue

                if images:
                    log.info(f"[Anonymous] [{elapsed:.0f}s] 检测到 {len(images)} 张图片")
                    # 返回图片 URL
                    image_urls = [f"![image]({img['src']})" for img in images]
                    return "\n".join(image_urls)

                # 检查是否有进度（生成状态或图片数量变化）
                if is_generating or images:
                    no_progress_count = 0
                else:
                    no_progress_count += 1
                    # 超过 30 秒没有进度，检查错误
                    if no_progress_count > 10:
                        error_text = await self._page.evaluate("""() => {
                            const errors = document.querySelectorAll('[class*="error"], [class*="fail"]');
                            for (const el of errors) { const t = el.textContent?.trim(); if (t && t.length > 5) return t.substring(0, 200); }
                            return null;
                        }""")
                        if error_text:
                            log.warning(f"[Anonymous] 检测到错误: {error_text}")
            else:
                # 普通文本回复模式
                content = await self._get_reply_text()

                # 过滤掉用户发送的消息
                if content and user_message and content.strip() == user_message.strip():
                    content = None

                # 检查生成状态
                is_gen = await self._is_generating()

                if content and content != last_content:
                    log.info(f"[Anonymous] [{elapsed:.0f}s] 内容变化: {content[:80]}...")
                    last_content = content
                    stable_count = 0
                elif content and content == last_content:
                    stable_count += 1
                    log.info(f"[Anonymous] [{elapsed:.0f}s] 内容稳定 ({stable_count}/3)")
                    if stable_count >= 3:
                        log.info(f"[Anonymous] 回复完成")
                        return content
                else:
                    log.info(f"[Anonymous] [{elapsed:.0f}s] 等待中... (gen={is_gen}, content={'有' if content else '无'})")

        log.warning(f"[Anonymous] 等待回复超时 ({timeout_sec}s)")
        return last_content if last_content else None

    # ── 公开接口 ──

    async def _ensure_mode(self, mode: str | None, image_options: dict | None = None) -> bool:
        """确保处于正确的模式

        Args:
            mode: "image" 切换到图片生成模式，None 为普通聊天
            image_options: 图片选项，包含比例等信息
        """
        if mode == "image":
            return await self._select_image_mode(image_options)
        return True

    async def _handle_upload(self, file_path: str | None) -> bool:
        """处理文件上传（如果提供了文件路径）

        Args:
            file_path: 要上传的文件路径，None 表示不上传
        """
        if not file_path:
            return True

        if not await self._upload_file(file_path):
            return False

        # 验证上传状态
        return await self._verify_upload()

    async def chat(
        self,
        message: str,
        timeout_sec: int = 120,
        mode: str | None = None,
        file_path: str | None = None,
        image_options: dict | None = None,
    ) -> AnonymousResponse:
        """发送消息并等待完整回复

        Args:
            message: 文本消息
            timeout_sec: 超时时间（秒）
            mode: "image" 切换到图片生成模式，None 为普通聊天
            file_path: 要上传的文件路径（支持图片、文档等），None 表示不上传
            image_options: 图片选项，包含比例等信息
        """
        if not await self._ensure_ready():
            return AnonymousResponse(content="", success=False, error="浏览器启动失败")

        # 从池中获取页签
        page = await self._acquire_tab()
        if not page:
            return AnonymousResponse(content="", success=False, error="无法获取页签")
        self._page = page

        try:
            return await self._do_chat(message, timeout_sec, mode, file_path, image_options)
        finally:
            self._page = None

    async def _do_chat(
        self,
        message: str,
        timeout_sec: int = 120,
        mode: str | None = None,
        file_path: str | None = None,
        image_options: dict  | None = None,
    ) -> AnonymousResponse:
        """chat 核心逻辑（页签已从池中获取）"""
        # 关闭弹窗/遮罩（在输入前）
        await self._dismiss_login_popup()

        # 切换模式
        if not await self._ensure_mode(mode, image_options):
            return AnonymousResponse(content="", success=False, error="切换图片生成模式失败")

        # 上传文件（多模态）
        if not await self._handle_upload(file_path):
            return AnonymousResponse(content="", success=False, error="文件上传失败")

        if not await self._type_message(message):
            return AnonymousResponse(content="", success=False, error="输入失败")

        if not await self._send_message():
            return AnonymousResponse(content="", success=False, error="发送失败")

        # 处理登录弹窗
        try:
            await self._dismiss_login_popup()
        except Exception as e:
            log.error(f"[Anonymous] 登录弹窗处理异常: {e}")

        # 图片生成和文件上传等待时间更长
        wait = timeout_sec
        if mode == "image":
            wait = max(timeout_sec, 180)
        elif file_path:
            wait = max(timeout_sec, 150)

        log.info(f"[Anonymous] 等待回复，超时: {wait}s")
        try:
            reply = await self._wait_reply(wait, user_message=message, mode=mode)
            log.info(f"[Anonymous] 等待回复完成: {reply[:50] if reply else 'None'}...")
        except Exception as e:
            log.error(f"[Anonymous] 等待回复异常: {e}")
            reply = None
        if reply:
            return AnonymousResponse(content=reply, success=True)
        else:
            return AnonymousResponse(content="", success=False, error="回复超时或为空")

    async def chat_stream(
        self,
        message: str,
        timeout_sec: int = 120,
        mode: str | None = None,
        file_path: str | None = None,
        image_options: dict | None = None,
    ) -> AsyncIterator[dict]:
        """流式聊天 — yield {"content": str} / {"done": True} / {"error": str}

        Args:
            message: 文本消息
            timeout_sec: 超时时间（秒）
            mode: "image" 切换到图片生成模式，None 为普通聊天
            file_path: 要上传的文件路径（支持图片、文档等），None 表示不上传
            image_options: 图片选项，包含比例等信息
        """
        log.info(f"[Anonymous] chat_stream 开始 mode={mode} timeout={timeout_sec}s")
        if not await self._ensure_ready():
            yield {"error": "浏览器启动失败"}
            return

        # 从池中获取页签
        page = await self._acquire_tab()
        if not page:
            yield {"error": "无法获取页签"}
            return
        self._page = page

        try:
            async for chunk in self._do_stream(message, timeout_sec, mode, file_path, image_options):
                yield chunk
        finally:
            self._page = None

    async def _do_stream(
        self,
        message: str,
        timeout_sec: int = 120,
        mode: str | None = None,
        file_path: str | None = None,
        image_options: dict | None = None,
    ) -> AsyncIterator[dict]:
        """chat_stream 核心逻辑 — 真流式（页签已从池中获取）"""
        # 关闭弹窗/遮罩（在输入前）
        await self._dismiss_login_popup()

        # 切换模式
        if not await self._ensure_mode(mode, image_options):
            yield {"error": "切换图片生成模式失败"}
            return

        # 上传文件（多模态）
        if not await self._handle_upload(file_path):
            yield {"error": "文件上传失败"}
            return

        if not await self._type_message(message):
            yield {"error": "输入失败"}
            return

        if not await self._send_message():
            yield {"error": "发送失败"}
            return

        # 处理登录弹窗
        await self._dismiss_login_popup()

        # 图片生成和文件上传等待时间更长
        wait = timeout_sec
        if mode == "image":
            wait = max(timeout_sec, 180)
        elif file_path:
            wait = max(timeout_sec, 150)

        log.info(f"[Anonymous] chat_stream 真流式等待 wait={wait}s")

        # 真流式：MutationObserver + 增量 yield
        try:
            async for chunk in self._wait_reply_stream(wait, user_message=message, mode=mode):
                yield chunk
        except Exception as e:
            log.error(f"[Anonymous] chat_stream 流式异常: {e}")
            yield {"error": str(e)}


# ── 全局单例 ──
_anonymous_client: Optional[QwenAnonymousClient] = None
_client_lock = asyncio.Lock()


async def get_anonymous_client() -> QwenAnonymousClient:
    """获取全局匿名客户端实例"""
    global _anonymous_client
    async with _client_lock:
        if _anonymous_client is None:
            _anonymous_client = QwenAnonymousClient()
        return _anonymous_client


async def close_anonymous_client():
    """关闭全局匿名客户端"""
    global _anonymous_client
    async with _client_lock:
        if _anonymous_client:
            await _anonymous_client.close()
            _anonymous_client = None
