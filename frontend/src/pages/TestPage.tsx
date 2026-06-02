import { useEffect, useRef, useState, useCallback, type ReactNode } from "react"
import { Button } from "../components/ui/button"
import { Send, RefreshCw, Bot, X, Wand2, Plus, Paperclip, Microscope, Video, Code, Presentation, Search, Copy, Check } from "lucide-react"
import { getAuthHeader } from "../lib/auth"
import { API_BASE } from "../lib/api"
import { toast } from "sonner"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter"
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism"

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const codeTheme = oneDark as any

// ─── 类型定义 ───────────────────────────────────────────────────────────────

interface AttachedImage {
  file: File
  preview: string       // object URL for display
  base64?: string       // data: URI for sending
}

interface ContentPart {
  type: string
  text?: string
  image_url?: { url: string }
}

type MessageContent = string | ContentPart[]

interface ChatMessage {
  role: string
  content: MessageContent
  reasoning?: string
  error?: boolean
}

interface ImageGenerationResponse {
  data?: { url?: string; revised_prompt?: string }[]
  detail?: unknown
  error?: unknown
}

type ModelCapability = {
  thinking?: boolean
  search?: boolean
  vision?: boolean
  deep_research?: boolean
  image_gen?: boolean
  video_gen?: boolean
  web_dev?: boolean
  slides?: boolean
}

type ModelOption = {
  id: string
  base_model?: string
  family?: string
  mode?: string
  display_name?: string
  capabilities?: ModelCapability
}

// ─── 工具函数 ───────────────────────────────────────────────────────────────

/** 压缩图片到合理大小，返回 data: URI */
async function compressImage(file: File, maxDim = 1024, quality = 0.8): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => {
      let { width, height } = img
      if (width > maxDim || height > maxDim) {
        const ratio = Math.min(maxDim / width, maxDim / height)
        width = Math.round(width * ratio)
        height = Math.round(height * ratio)
      }
      const canvas = document.createElement("canvas")
      canvas.width = width
      canvas.height = height
      const ctx = canvas.getContext("2d")
      if (!ctx) { reject(new Error("Canvas not supported")); return }
      ctx.drawImage(img, 0, 0, width, height)
      const mimeType = file.type === "image/png" ? "image/png" : "image/jpeg"
      resolve(canvas.toDataURL(mimeType, quality))
    }
    img.onerror = () => reject(new Error("Failed to load image"))
    img.src = URL.createObjectURL(file)
  })
}

/** 从消息内容中提取纯文本 */
function extractText(content: MessageContent): string {
  if (typeof content === "string") return content
  return content.filter(p => p.type === "text").map(p => p.text || "").join("")
}

/** 从消息内容中提取图片 URL 列表 */
function extractImageUrls(content: MessageContent): string[] {
  if (typeof content === "string") return []
  return content
    .filter(p => p.type === "image_url" && p.image_url?.url)
    .map(p => p.image_url!.url)
}

// ─── 模型相关工具函数 ──────────────────────────────────────────────────────

const MODEL_MODE_SUFFIX_RE = /-(thinking|deep-research|deep_research|image|video|webdev|web-dev|slides|t2i|t2v)$/i
const CAPABILITY_LABELS: Array<{ key: keyof ModelCapability; label: string }> = [
  { key: "thinking", label: "思考" },
  { key: "search", label: "搜索" },
  { key: "vision", label: "视觉" },
  { key: "deep_research", label: "研究" },
  { key: "image_gen", label: "图片" },
  { key: "video_gen", label: "视频" },
  { key: "web_dev", label: "建站" },
  { key: "slides", label: "PPT" },
]

function asText(value: unknown): string {
  return typeof value === "string" ? value : ""
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {}
}

function normalizeModelOption(value: unknown): ModelOption | null {
  if (typeof value === "string" && value) return { id: value, capabilities: {} }
  const record = asRecord(value)
  const id = asText(record.id)
  if (!id) return null
  return {
    id,
    base_model: asText(record.base_model) || undefined,
    family: asText(record.family) || undefined,
    mode: asText(record.mode) || undefined,
    display_name: asText(record.display_name) || undefined,
    capabilities: asRecord(record.capabilities) as ModelCapability,
  }
}

function isBaseModelOption(option: ModelOption): boolean {
  return option.base_model ? option.id === option.base_model : !MODEL_MODE_SUFFIX_RE.test(option.id)
}

function isThinkingVariant(modelId: string): boolean {
  return /-thinking$/i.test(modelId)
}

function capabilityBadges(option?: ModelOption): string[] {
  if (!option?.capabilities) return []
  return CAPABILITY_LABELS.filter(item => option.capabilities?.[item.key]).map(item => item.label)
}

function formatModelOption(option: ModelOption): string {
  return option.display_name || option.id
}

const FEATURE_MODES: Array<{ mode: InputMode; label: string; icon: typeof Microscope; capKey: keyof ModelCapability }> = [
  { mode: "deep_research", label: "深入研究", icon: Microscope, capKey: "deep_research" },
  { mode: "video",         label: "创建视频", icon: Video,      capKey: "video_gen" },
  { mode: "web_dev",       label: "网页开发", icon: Code,       capKey: "web_dev" },
  { mode: "slides",        label: "幻灯片",   icon: Presentation, capKey: "slides" },
  { mode: "search",        label: "网页搜索", icon: Search,     capKey: "search" },
]

function findModelByCapability(models: ModelOption[], capKey: keyof ModelCapability): ModelOption | undefined {
  return models.find(m => m.capabilities?.[capKey])
}

function chooseDefaultModel(options: ModelOption[], currentModel?: string): string {
  if (currentModel && options.some(option => option.id === currentModel)) return currentModel
  const preferred = options.find(option => option.id === "qwen3.6-plus")
  if (preferred) return preferred.id
  const base = options.find(isBaseModelOption)
  return base?.id || options[0]?.id || "qwen3.6-plus"
}


function extractTextFromContent(content: unknown): string {
  if (typeof content === "string") return content
  if (!Array.isArray(content)) return ""
  return content
    .map(part => {
      const block = asRecord(part)
      const type = asText(block.type)
      if (type === "thinking" || type === "reasoning" || type === "reasoning_text") {
        return ""
      }
      if (type === "text" || type === "output_text" || type === "message") {
        return asText(block.text) || asText(block.content)
      }
      return asText(block.text) || asText(block.content)
    })
    .join("")
}

function readReasoningFields(value: unknown): string {
  const record = asRecord(value)
  const extra = asRecord(record.extra)
  return (
    asText(record.reasoning_content) ||
    asText(record.reasoning) ||
    asText(record.reasoning_text) ||
    asText(record.thinking) ||
    asText(record.thoughts) ||
    asText(extra.reasoning_content) ||
    asText(extra.reasoning) ||
    asText(extra.reasoning_text) ||
    asText(extra.thinking) ||
    asText(extra.thoughts)
  )
}

function splitInlineThinking(content: string, reasoning = ""): { content: string; reasoning: string } {
  if (!content || !/<think[\s>]/i.test(content)) return { content, reasoning }
  let visible = ""
  let thoughts = reasoning
  let cursor = 0
  for (const match of content.matchAll(/<think[^>]*>([\s\S]*?)<\/think>/gi)) {
    visible += content.slice(cursor, match.index)
    thoughts += match[1] || ""
    cursor = (match.index ?? 0) + match[0].length
  }
  visible += content.slice(cursor)
  return { content: visible, reasoning: thoughts }
}


function extractReasoningFromContent(content: unknown): string {
  if (!Array.isArray(content)) return ""
  return content
    .map(part => {
      const block = asRecord(part)
      const type = block.type
      if (type === "thinking") return asText(block.thinking)
      if (type === "reasoning_text") return asText(block.text)
      if (type === "reasoning") return asText(block.text) || asText(block.reasoning)
      return readReasoningFields(block)
    })
    .join("")
}

function normalizeAssistantMessage(message: unknown): ChatMessage {
  const msg = asRecord(message)
  const inline = splitInlineThinking(extractTextFromContent(msg.content), readReasoningFields(msg) || extractReasoningFromContent(msg.content))
  return {
    role: asText(msg.role) || "assistant",
    content: inline.content,
    ...(inline.reasoning ? { reasoning: inline.reasoning } : {}),
  }
}

function extractStreamDelta(payload: unknown): { content: string; reasoning: string } {
  const data = asRecord(payload)
  const responseEventType = asText(data.type)
  if (responseEventType === "response.reasoning_text.delta") {
    return { content: "", reasoning: asText(data.delta) }
  }
  if (responseEventType === "response.output_text.delta") {
    return splitInlineThinking(asText(data.delta))
  }

  const choices = Array.isArray(data.choices) ? data.choices : []
  const choice = asRecord(choices[0])
  const delta = asRecord(choice.delta)
  const message = asRecord(choice.message)
  const content = extractTextFromContent(delta.content) || extractTextFromContent(message.content) || extractTextFromContent(data.content)
  const reasoning = readReasoningFields(delta) || readReasoningFields(message) || readReasoningFields(data) || extractReasoningFromContent(delta.content) || extractReasoningFromContent(message.content)
  return splitInlineThinking(content, reasoning)
}

// ─── 消息内容渲染组件 ──────────────────────────────────────────────────────

/** 代码块：带语言标签和复制按钮 */
function CodeBlock({ className, children, ...props }: React.HTMLAttributes<HTMLElement> & { children?: ReactNode }) {
  const [copied, setCopied] = useState(false)
  const match = /language-(\w+)/.exec(className || "")
  const lang = match?.[1] || ""
  const code = String(children).replace(/\n$/, "")

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      // fallback
      const ta = document.createElement("textarea")
      ta.value = code
      document.body.appendChild(ta)
      ta.select()
      document.execCommand("copy")
      document.body.removeChild(ta)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    }
  }

  if (match) {
    return (
      <div className="code-block-wrapper">
        {lang && <div className="code-lang-label">{lang}</div>}
        <button className="code-copy-btn" onClick={handleCopy} title="复制代码">
          {copied ? <Check className="inline w-3.5 h-3.5" /> : <Copy className="inline w-3.5 h-3.5" />}
        </button>
        <SyntaxHighlighter
          style={codeTheme}
          language={lang}
          PreTag="div"
          customStyle={{ margin: 0, borderRadius: "0.5rem", fontSize: "0.88em", paddingTop: lang ? "1.8em" : "1em" }}
          {...props}
        >
          {code}
        </SyntaxHighlighter>
      </div>
    )
  }
  // 行内代码走默认渲染
  return <code className={className} {...props}>{children}</code>
}

function MessageContent({ content, onPreview }: { content: MessageContent; onPreview?: (url: string) => void }) {
  const text = extractText(content)

  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code: (props) => <CodeBlock {...props} />,
          img: ({ src, alt }) => (
            <div className="my-2">
              <img
                src={src}
                alt={alt || "image"}
                className="max-w-full rounded-lg shadow-md border cursor-pointer hover:opacity-90 transition-opacity"
                loading="lazy"
                onClick={() => src && onPreview?.(src)}
                onError={e => { (e.currentTarget as HTMLImageElement).style.display = "none" }}
              />
            </div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}

/** 用户消息渲染：支持多模态（文本 + 图片） */
function UserMessageDisplay({ content, onPreview }: { content: MessageContent; onPreview?: (url: string) => void }) {
  const images = extractImageUrls(content)
  const text = extractText(content)

  return (
    <div className="space-y-2">
      {images.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {images.map((url, i) => (
            <img
              key={i}
              src={url}
              alt={`attached-${i}`}
              className="max-w-[200px] max-h-[150px] rounded-lg border object-cover cursor-pointer hover:scale-105 transition-transform"
              onClick={() => onPreview?.(url)}
            />
          ))}
        </div>
      )}
      {text && <div className="whitespace-pre-wrap leading-relaxed">{text}</div>}
    </div>
  )
}

// ─── 常量 ────────────────────────────────────────────────────────────────────

const TYPEWRITER_CHUNK_SIZE = 2
const TYPEWRITER_DELAY_MS = 24
const FALLBACK_MODELS: ModelOption[] = [{ id: "qwen3.6-plus", base_model: "qwen3.6-plus", family: "qwen3.6", mode: "chat", capabilities: {} }]

const ASPECT_RATIOS = [
  { label: "1:1",  value: "1:1"  },
  { label: "16:9", value: "16:9" },
  { label: "9:16", value: "9:16" },
  { label: "4:3",  value: "4:3"  },
  { label: "3:4",  value: "3:4"  },
]

type InputMode = "chat" | "image" | "deep_research" | "video" | "web_dev" | "slides" | "search"

// ─── 主页面组件 ─────────────────────────────────────────────────────────────

export default function TestPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState("")
  const [attachedImages, setAttachedImages] = useState<AttachedImage[]>([])
  const [loading, setLoading] = useState(false)
  const [model, setModel] = useState("qwen3.6-plus")
  const [availableModels, setAvailableModels] = useState<ModelOption[]>(FALLBACK_MODELS)
  const [stream, setStream] = useState(true)
  const [typewriter, setTypewriter] = useState(false)
  const [answerMode, setAnswerMode] = useState<"auto" | "thinking" | "fast">("auto")
  const [inputMode, setInputMode] = useState<InputMode>("chat")
  const [imageRatio, setImageRatio] = useState("16:9")
  const [showMenu, setShowMenu] = useState(false)
  const [previewImage, setPreviewImage] = useState<string | null>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const dropZoneRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const selectedForcesThinking = isThinkingVariant(model)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" })
  }, [messages])

  // 挂载时从 /v1/models 拉模型列表
  useEffect(() => {
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/v1/models`, { headers: getAuthHeader() })
        if (!r.ok) return
        const j = await r.json()
        const options = (j?.data || [])
          .map(normalizeModelOption)
          .filter((item: ModelOption | null): item is ModelOption => Boolean(item?.id))
        if (options.length) {
          setAvailableModels(options)
          setModel(current => chooseDefaultModel(options, current))
        }
      } catch {
        // keep fallback list
      }
    })()
  }, [])

  // 点击外部关闭菜单
  useEffect(() => {
    if (!showMenu) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setShowMenu(false)
      }
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [showMenu])

  // ─── 图片处理 ──────────────────────────────────────────────────────────

  const addImages = useCallback(async (files: FileList | File[]) => {
    const newImages: AttachedImage[] = []
    for (const file of Array.from(files)) {
      if (!file.type.startsWith("image/")) continue
      if (file.size > 20 * 1024 * 1024) {
        toast.error(`${file.name} 超过 20MB 限制`)
        continue
      }
      const preview = URL.createObjectURL(file)
      try {
        const base64 = await compressImage(file)
        newImages.push({ file, preview, base64 })
      } catch {
        toast.error(`${file.name} 处理失败`)
        URL.revokeObjectURL(preview)
      }
    }
    if (newImages.length) {
      setAttachedImages(prev => [...prev, ...newImages])
    }
  }, [])

  const removeImage = useCallback((index: number) => {
    setAttachedImages(prev => {
      const removed = prev[index]
      if (removed) URL.revokeObjectURL(removed.preview)
      return prev.filter((_, i) => i !== index)
    })
  }, [])

  // 拖拽处理
  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dropZoneRef.current?.classList.add("ring-2", "ring-primary/50")
  }, [])

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dropZoneRef.current?.classList.remove("ring-2", "ring-primary/50")
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    dropZoneRef.current?.classList.remove("ring-2", "ring-primary/50")
    if (e.dataTransfer.files.length) {
      addImages(e.dataTransfer.files)
    }
  }, [addImages])

  // 粘贴处理
  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = Array.from(e.clipboardData.items)
    const imageFiles = items
      .filter(item => item.type.startsWith("image/"))
      .map(item => item.getAsFile())
      .filter((f): f is File => f !== null)
    if (imageFiles.length) {
      e.preventDefault()
      addImages(imageFiles)
    }
  }, [addImages])

  // ─── 构建消息内容 ──────────────────────────────────────────────────────

  const buildContentParts = useCallback(async (text: string, images: AttachedImage[]): Promise<MessageContent> => {
    if (images.length === 0) return text
    const parts: ContentPart[] = []
    if (text.trim()) {
      parts.push({ type: "text", text })
    }
    for (const img of images) {
      const dataUri = img.base64 || await compressImage(img.file)
      parts.push({
        type: "image_url",
        image_url: { url: dataUri },
      })
    }
    return parts
  }, [])

  // ─── 流式响应处理 ──────────────────────────────────────────────────────

  const appendAssistantDelta = (content: string, reasoning: string) => {
    if (!content && !reasoning) return
    setMessages(prev => {
      const msgs = [...prev]
      const last = msgs[msgs.length - 1] ?? { role: "assistant", content: "" }
      const currentText = typeof last.content === "string" ? last.content : extractText(last.content)
      msgs[msgs.length - 1] = {
        ...last,
        content: currentText + content,
        reasoning: (last.reasoning || "") + reasoning,
      }
      return msgs
    })
  }

  const appendAssistantTypewriter = async (message: ChatMessage) => {
    const content = typeof message.content === "string" ? message.content : extractText(message.content)
    const reasoning = message.reasoning || ""

    if (!typewriter) {
      setMessages(prev => [...prev, { role: "assistant", content, reasoning }])
      return
    }

    setMessages(prev => [...prev, { role: "assistant", content: "" }])
    let pendingReasoning = reasoning
    let pendingContent = content
    while (pendingReasoning || pendingContent) {
      if (pendingReasoning) {
        const chunk = pendingReasoning.slice(0, TYPEWRITER_CHUNK_SIZE)
        pendingReasoning = pendingReasoning.slice(chunk.length)
        appendAssistantDelta("", chunk)
      } else {
        const chunk = pendingContent.slice(0, TYPEWRITER_CHUNK_SIZE)
        pendingContent = pendingContent.slice(chunk.length)
        appendAssistantDelta(chunk, "")
      }
      await new Promise(resolve => window.setTimeout(resolve, TYPEWRITER_DELAY_MS))
    }
  }

  // ─── 图片生成 ──────────────────────────────────────────────────────────

  const handleImageGenerate = async (prompt: string) => {
    if (!prompt && attachedImages.length === 0) return
    setLoading(true)
    try {
      const requestBody: Record<string, unknown> = {
        model: "dall-e-3",
        prompt: prompt || "根据参考图生成",
        n: 1,
        ratio: imageRatio,
        response_format: "url",
      }

      if (attachedImages.length > 0) {
        const images: string[] = []
        for (const img of attachedImages) {
          const dataUri = img.base64 || await compressImage(img.file)
          images.push(dataUri)
        }
        requestBody.images = images
      }

      const res = await fetch(`${API_BASE}/v1/images/generations`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...getAuthHeader() },
        body: JSON.stringify(requestBody),
      })

      const data = (await res.json()) as ImageGenerationResponse
      if (!res.ok) {
        const detail = data?.detail || data?.error || `HTTP ${res.status}`
        toast.error(`生成失败: ${String(detail).slice(0, 80)}`)
        return
      }

      const urls = (data.data ?? [])
        .map(item => item.url)
        .filter((url): url is string => typeof url === "string" && url.length > 0)

      if (urls.length === 0) {
        toast.error("未返回图片，请重试")
        return
      }

      let userContent: MessageContent = `🎨 生成图片：${prompt || "根据参考图生成"}`
      if (attachedImages.length > 0) {
        const parts: ContentPart[] = []
        if (prompt) {
          parts.push({ type: "text", text: `🎨 生成图片：${prompt}` })
        } else {
          parts.push({ type: "text", text: "🎨 根据参考图生成" })
        }
        for (const img of attachedImages) {
          const dataUri = img.base64 || await compressImage(img.file)
          parts.push({ type: "image_url", image_url: { url: dataUri } })
        }
        userContent = parts
      }

      setMessages(prev => [
        ...prev,
        { role: "user", content: userContent },
        { role: "assistant", content: urls.map(url => `![generated](${url})`).join("\n") },
      ])
      setInput("")
      setAttachedImages([])
      toast.success(`成功生成 ${urls.length} 张图片`)
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "网络错误"
      toast.error(`生成失败: ${msg}`)
    } finally {
      setLoading(false)
    }
  }

  // ─── 发送消息（统一入口，根据 inputMode 分流）──────────────────────────

  const handleSend = async () => {
    const text = input.trim()
    if ((!text && attachedImages.length === 0) || loading) return

    // 图片生成模式
    if (inputMode === "image") {
      await handleImageGenerate(text)
      return
    }

    // 聊天模式
    const content = await buildContentParts(text, attachedImages)
    const userMsg: ChatMessage = { role: "user", content }
    const wantsThinking = answerMode === "thinking"
    const isAuto = answerMode === "auto"
    const requestBody: Record<string, unknown> = {
      model,
      messages: [...messages, userMsg],
      stream,
    }
    if (!isAuto) {
      requestBody.include_reasoning = wantsThinking
      requestBody.enable_thinking = wantsThinking
    }
    if (!wantsThinking && !isAuto && selectedForcesThinking) {
      toast.info("该模型为强制思考变体，快速模式不会生效")
    }
    setMessages(prev => [...prev, userMsg])
    setInput("")
    setAttachedImages([])
    setLoading(true)

    try {
      if (!stream) {
        const res = await fetch(`${API_BASE}/v1/chat/completions`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeader() },
          body: JSON.stringify({ ...requestBody, stream: false })
        })
        const data = await res.json()
        if (data.error) {
          setMessages(prev => [...prev, { role: "assistant", content: `❌ ${data.error}`, error: true }])
        } else if (data.choices?.[0]) {
          await appendAssistantTypewriter(normalizeAssistantMessage(data.choices[0].message))
        } else {
          setMessages(prev => [...prev, { role: "assistant", content: `❌ 未知响应: ${JSON.stringify(data)}`, error: true }])
        }
      } else {
        const res = await fetch(`${API_BASE}/v1/chat/completions`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...getAuthHeader() },
          body: JSON.stringify({ ...requestBody, stream: true })
        })

        if (!res.ok) {
          const errText = await res.text()
          setMessages(prev => [...prev, { role: "assistant", content: `❌ HTTP ${res.status}: ${errText}`, error: true }])
          return
        }

        if (!res.body) throw new Error("No response body")

        setMessages(prev => [...prev, { role: "assistant", content: "" }])
        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let hasContent = false
        let hasTerminalError = false
        const outputQueue = { content: "", reasoning: "" }
        let typewriterRunning = false

        const runTypewriter = async () => {
          if (typewriterRunning) return
          typewriterRunning = true
          try {
            while (outputQueue.reasoning || outputQueue.content) {
              if (outputQueue.reasoning) {
                const chunk = outputQueue.reasoning.slice(0, TYPEWRITER_CHUNK_SIZE)
                outputQueue.reasoning = outputQueue.reasoning.slice(chunk.length)
                appendAssistantDelta("", chunk)
              } else {
                const chunk = outputQueue.content.slice(0, TYPEWRITER_CHUNK_SIZE)
                outputQueue.content = outputQueue.content.slice(chunk.length)
                appendAssistantDelta(chunk, "")
              }
              await new Promise(resolve => window.setTimeout(resolve, TYPEWRITER_DELAY_MS))
            }
          } finally {
            typewriterRunning = false
            if (outputQueue.reasoning || outputQueue.content) void runTypewriter()
          }
        }

        const waitForTypewriter = async () => {
          while (typewriterRunning || outputQueue.reasoning || outputQueue.content) {
            await new Promise(resolve => window.setTimeout(resolve, 20))
          }
        }

        const enqueueAssistantDelta = (content: string, reasoning: string) => {
          if (!content && !reasoning) return
          hasContent = true
          if (typewriter) {
            outputQueue.content += content
            outputQueue.reasoning += reasoning
            void runTypewriter()
          } else {
            appendAssistantDelta(content, reasoning)
          }
        }

        let currentEventData = ""

        const processSsePayload = (payload: string) => {
          const trimmedPayload = payload.trim()
          if (!trimmedPayload || trimmedPayload === "[DONE]") return

          try {
            const data = JSON.parse(trimmedPayload)
            if (data.error) {
              outputQueue.content = ""
              outputQueue.reasoning = ""
              setMessages(prev => {
                const msgs = [...prev]
                msgs[msgs.length - 1] = { role: "assistant", content: `❌ ${data.error}`, error: true }
                return msgs
              })
              hasContent = true
              hasTerminalError = true
              return
            }
            const { content, reasoning } = extractStreamDelta(data)
            enqueueAssistantDelta(content, reasoning)
          } catch {
            // Keep the test page resilient to malformed payloads without aborting the stream.
          }
        }

        let buffer = ""

        const dispatchSseEvent = () => {
          if (!currentEventData) return
          const payload = currentEventData
          currentEventData = ""
          processSsePayload(payload)
        }

        const processSseLine = (rawLine: string) => {
          const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine
          if (line === "") {
            dispatchSseEvent()
            return
          }
          if (line.startsWith(":")) return
          if (!line.startsWith("data:")) return

          const data = line.startsWith("data: ") ? line.slice(6) : line.slice(5)
          currentEventData += currentEventData ? `\n${data}` : data
        }

        const processSseChunk = (chunk: string) => {
          if (!chunk) return
          buffer += chunk
          const lines = buffer.split("\n")
          buffer = lines.pop() ?? ""
          for (const line of lines) {
            processSseLine(line)
            if (hasTerminalError) break
          }
        }

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          processSseChunk(decoder.decode(value, { stream: true }))
          if (hasTerminalError) break
        }

        if (!hasTerminalError) {
          processSseChunk(decoder.decode())
          if (buffer) {
            processSseLine(buffer)
            buffer = ""
          }
          dispatchSseEvent()
        } else {
          decoder.decode()
        }

        if (typewriter) await waitForTypewriter()

        if (!hasContent) {
          setMessages(prev => {
            const msgs = [...prev]
            msgs[msgs.length - 1] = { role: "assistant", content: "❌ 响应为空（账号可能未激活或无可用账号）", error: true }
            return msgs
          })
        }
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "未知错误"
      toast.error(`网络错误: ${message}`)
      setMessages(prev => [...prev, { role: "assistant", content: `❌ 网络错误: ${message}`, error: true }])
    } finally {
      setLoading(false)
    }
  }

  // ─── 键盘快捷键 ──────────────────────────────────────────────────────

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // ─── 渲染 ─────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col h-[calc(100vh-10rem)] space-y-4 max-w-5xl mx-auto">
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        multiple
        className="hidden"
        onChange={e => {
          if (e.target.files) addImages(e.target.files)
          e.target.value = ""
        }}
      />

      {/* 精简 header */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <h2 className="text-lg font-bold tracking-tight">接口测试</h2>
          <div className="flex items-center gap-2 rounded-xl border bg-card/80 px-3 py-1.5 text-sm">
            <select value={model} onChange={e => setModel(e.target.value)} className="max-w-[16rem] bg-transparent font-mono outline-none text-sm">
              {availableModels.filter(isBaseModelOption).map(option => (
                <option key={option.id} value={option.id}>{formatModelOption(option)}</option>
              ))}
            </select>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <label className="flex cursor-pointer items-center gap-1.5 rounded-lg border bg-card/80 px-2.5 py-1.5 text-sm">
            <input type="checkbox" checked={stream} onChange={() => setStream(!stream)} className="cursor-pointer" />
            <span>流式</span>
          </label>
          <label className="flex cursor-pointer items-center gap-1.5 rounded-lg border bg-card/80 px-2.5 py-1.5 text-sm">
            <input type="checkbox" checked={typewriter} onChange={() => setTypewriter(!typewriter)} className="cursor-pointer" />
            <span>打字机</span>
          </label>
          <Button variant="outline" size="sm" onClick={() => { setMessages([]); setInput(""); setAttachedImages([]) }}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> 新建
          </Button>
        </div>
      </div>

      <div className="flex-1 rounded-xl border bg-card overflow-hidden flex flex-col shadow-sm">
        <div className="flex-1 overflow-y-auto p-6 space-y-6 flex flex-col">
          {messages.length === 0 && (
            <div className="h-full flex flex-col items-center justify-center text-muted-foreground space-y-4">
              <Bot className="h-12 w-12 text-muted-foreground/30" />
              <div className="text-center space-y-1">
                <p className="text-sm">发送消息以开始测试，支持多模态输入。</p>
                <p className="text-xs text-muted-foreground/60">
                  📎 附加图片 · 🎨 生成图片 · 💬 文本聊天
                </p>
              </div>
            </div>
          )}
          {messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[80%] rounded-xl px-4 py-3 text-sm shadow-sm
                ${msg.role === "user"
                  ? "bg-primary text-primary-foreground"
                  : msg.error
                    ? "bg-red-500/10 border border-red-500/30 text-red-400"
                    : "bg-muted/30 border text-foreground"}`}>
                {msg.role === "user" ? (
                  <UserMessageDisplay content={msg.content} onPreview={setPreviewImage} />
                ) : msg.role === "assistant" && !msg.content && !msg.reasoning && loading ? (
                  <span className="animate-pulse flex items-center gap-2 text-muted-foreground">
                    <Bot className="h-4 w-4" /> 思考中...
                  </span>
                ) : msg.role === "assistant" && !msg.error ? (
                  <div className="space-y-2">
                    {msg.reasoning ? (
                      <details open className="rounded-md border border-dashed border-border/50 bg-muted/20 p-2 text-xs">
                        <summary className="cursor-pointer select-none text-muted-foreground font-mono">
                          💭 思考过程 ({msg.reasoning.length} 字)
                        </summary>
                        <div className="whitespace-pre-wrap leading-relaxed text-muted-foreground mt-2 pl-2 border-l-2 border-border/30">
                          {msg.reasoning}
                        </div>
                      </details>
                    ) : null}
                    {msg.content ? <MessageContent content={msg.content} onPreview={setPreviewImage} /> : null}
                  </div>
                ) : (
                  <div className="whitespace-pre-wrap leading-relaxed">{extractText(msg.content)}</div>
                )}
              </div>
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        {/* 统一输入框容器 */}
        <div
          ref={dropZoneRef}
          className={`mx-4 mb-4 rounded-2xl border transition-all ${
            inputMode !== "chat" ? "border-primary/30 bg-primary/5" : "border-border bg-background"
          }`}
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          {/* 附件预览区域（在输入框内部上方） */}
          {attachedImages.length > 0 && (
            <div className="px-3 pt-3 pb-1">
              <div className="flex flex-wrap gap-2">
                {attachedImages.map((img, i) => (
                  <div key={i} className="relative group">
                    <img
                      src={img.preview}
                      alt={`attach-${i}`}
                      className="h-14 w-14 object-cover rounded-lg border"
                    />
                    <button
                      onClick={() => removeImage(i)}
                      className="absolute -top-1.5 -right-1.5 bg-red-500 text-white rounded-full p-0.5 opacity-0 group-hover:opacity-100 transition-opacity shadow-sm"
                    >
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* textarea 输入区 */}
          <div className="flex items-end px-3 py-2">
            <textarea
              ref={inputRef}
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              rows={1}
              className="flex-1 bg-transparent border-0 outline-none resize-none text-sm py-1 px-1 min-h-[24px] max-h-[120px] placeholder:text-muted-foreground/60"
              placeholder={
                inputMode === "image"
                  ? "描述你想生成的图片..."
                  : inputMode !== "chat"
                    ? `输入内容，使用${FEATURE_MODES.find(f => f.mode === inputMode)?.label || ""}模式...`
                    : attachedImages.length > 0
                      ? "添加说明或直接发送图片..."
                      : "输入消息..."
              }
              disabled={loading}
              style={{ height: 'auto' }}
              onInput={e => {
                const target = e.target as HTMLTextAreaElement
                target.style.height = 'auto'
                target.style.height = Math.min(target.scrollHeight, 120) + 'px'
              }}
            />
          </div>

          {/* 底部工具栏 */}
          <div className="flex items-center justify-between px-2 pb-2 pt-0">
            <div className="flex items-center gap-1">
              {/* + 按钮 + 弹出菜单 */}
              <div className="relative" ref={menuRef}>
                <button
                  className={`p-2 rounded-lg transition-colors ${
                    showMenu ? "bg-muted text-foreground" : "text-muted-foreground hover:text-foreground hover:bg-muted/50"
                  }`}
                  onClick={() => setShowMenu(!showMenu)}
                  title="更多操作"
                >
                  <Plus className={`h-4 w-4 transition-transform duration-200 ${showMenu ? "rotate-45" : ""}`} />
                </button>

                {/* 弹出菜单 */}
                {showMenu && (
                  <div className="absolute bottom-full left-0 mb-2 w-48 rounded-xl border bg-card shadow-lg overflow-hidden animate-in fade-in slide-in-from-bottom-1 duration-150">
                    {/* 附件上传 */}
                    <button
                      className="flex items-center gap-3 w-full px-4 py-2.5 text-sm text-foreground hover:bg-muted/60 transition-colors"
                      onClick={() => {
                        setShowMenu(false)
                        fileInputRef.current?.click()
                      }}
                    >
                      <Paperclip className="h-4 w-4 text-muted-foreground" />
                      <span>附件上传</span>
                    </button>
                    <div className="border-t border-border/50" />
                    {/* 图片生成 */}
                    <button
                      className={`flex items-center gap-3 w-full px-4 py-2.5 text-sm transition-colors ${
                        inputMode === "image"
                          ? "text-primary bg-primary/5 hover:bg-primary/10"
                          : "text-foreground hover:bg-muted/60"
                      }`}
                      onClick={() => {
                        setShowMenu(false)
                        if (inputMode === "image") {
                          setInputMode("chat")
                        } else {
                          setInputMode("image")
                        }
                      }}
                    >
                      <Wand2 className={`h-4 w-4 ${inputMode === "image" ? "text-primary" : "text-muted-foreground"}`} />
                      <span>图片生成</span>
                    </button>
                    {/* 功能模式 */}
                    {FEATURE_MODES.map(f => (
                      <button
                        key={f.mode}
                        className={`flex items-center gap-3 w-full px-4 py-2.5 text-sm transition-colors ${
                          inputMode === f.mode
                            ? "text-primary bg-primary/5 hover:bg-primary/10"
                            : "text-foreground hover:bg-muted/60"
                        }`}
                        onClick={() => {
                          setShowMenu(false)
                          if (inputMode === f.mode) {
                            setInputMode("chat")
                            return
                          }
                          const target = findModelByCapability(availableModels, f.capKey)
                          if (target) {
                            setModel(target.id)
                            setInputMode(f.mode)
                          } else {
                            toast.info(`没有可用的「${f.label}」模型`)
                          }
                        }}
                      >
                        <f.icon className="h-4 w-4 text-muted-foreground" />
                        <span>{f.label}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>

              {/* 当前功能模式标签 */}
              {inputMode !== "chat" && (
                <button
                  className="flex items-center gap-1 px-2 py-1 ml-1 rounded-md bg-primary/10 text-primary hover:bg-primary/15 transition-colors"
                  onClick={() => setInputMode("chat")}
                  title="点击退出此模式"
                >
                  {inputMode === "image" ? (
                    <Wand2 className="h-3 w-3" />
                  ) : (() => {
                    const Icon = FEATURE_MODES.find(f => f.mode === inputMode)?.icon
                    return Icon ? <Icon className="h-3 w-3" /> : null
                  })()}
                  <span className="text-xs font-medium">
                    {inputMode === "image" ? "图片生成" : FEATURE_MODES.find(f => f.mode === inputMode)?.label}
                  </span>
                  <X className="h-2.5 w-2.5 opacity-60" />
                </button>
              )}
              {/* 比例选择（仅图片模式显示） */}
              {inputMode === "image" && (
                <select
                  value={imageRatio}
                  onChange={e => setImageRatio(e.target.value)}
                  className="ml-1 bg-primary/10 border-0 rounded-md text-xs font-medium text-primary px-2 py-1 outline-none cursor-pointer hover:bg-primary/15 transition-colors"
                  disabled={loading}
                >
                  {ASPECT_RATIOS.map(r => (
                    <option key={r.value} value={r.value}>{r.label}</option>
                  ))}
                </select>
              )}
            </div>

            <div className="flex items-center gap-1">
              {/* 模式选择 */}
              <select
                value={answerMode}
                onChange={e => setAnswerMode(e.target.value as "auto" | "thinking" | "fast")}
                disabled={loading}
                className="bg-muted/60 border-0 rounded-lg text-xs font-medium px-2.5 py-1.5 outline-none cursor-pointer hover:bg-muted transition-colors"
              >
                <option value="auto">自动</option>
                <option value="thinking">思考</option>
                <option value="fast">快速</option>
              </select>

              {/* 发送按钮 */}
              <button
                onClick={handleSend}
                disabled={loading || (!input.trim() && attachedImages.length === 0)}
                className={`p-2 rounded-lg transition-all ${
                  loading || (!input.trim() && attachedImages.length === 0)
                    ? "text-muted-foreground/40 cursor-not-allowed"
                    : inputMode === "image"
                      ? "bg-primary text-primary-foreground hover:bg-primary/90"
                      : "bg-foreground text-background hover:bg-foreground/90"
                }`}
              >
                {loading ? (
                  <RefreshCw className="h-4 w-4 animate-spin" />
                ) : inputMode === "image" ? (
                  <Wand2 className="h-4 w-4" />
                ) : (
                  <Send className="h-4 w-4" />
                )}
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* 图片预览弹窗 */}
      {previewImage && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
          onClick={() => setPreviewImage(null)}
        >
          <div className="relative max-w-[90vw] max-h-[90vh]" onClick={e => e.stopPropagation()}>
            <button
              onClick={() => setPreviewImage(null)}
              className="absolute -top-3 -right-3 z-10 p-1.5 rounded-full bg-background/90 border shadow-lg hover:bg-background transition-colors"
            >
              <X className="h-4 w-4" />
            </button>
            <img
              src={previewImage}
              alt="preview"
              className="max-w-full max-h-[85vh] rounded-lg shadow-2xl object-contain"
            />
          </div>
        </div>
      )}
    </div>
  )
}
