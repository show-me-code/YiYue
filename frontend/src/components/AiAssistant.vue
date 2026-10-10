<script setup lang="ts">
/** AI 阅读助手：基于当前论文的多轮问答，含快速开始模板与会话管理。
 *  助手回复按 Markdown + KaTeX 渲染（LLM 的回答通常是 Markdown 并含 $...$ 公式）。 */
import { nextTick, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import { api, type ChatMessage } from '../api/client'

const props = defineProps<{ taskId: number }>()
const emit = defineEmits<{ close: [] }>()

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

/** Markdown 渲染 + 公式：先把 $...$ / $$...$$ 换成占位符（防 markdown 破坏公式），
 *  Markdown 渲染后再用 KaTeX 换回排版好的公式。 */
function renderMd(text: string): string {
  const maths: string[] = []
  const stashed = text.replace(
    /\$\$[\s\S]+?\$\$|\$(?:\\.|[^$\\])+\$/g,
    (m) => {
      maths.push(m)
      return `XQMATHX${maths.length - 1}XQ`
    })
  const html = md.render(stashed)
  return html.replace(/XQMATHX(\d+)XQ/g, (_m, i) => {
    const raw = maths[Number(i)]
    const display = raw.startsWith('$$')
    return katex.renderToString(raw.slice(display ? 2 : 1, display ? -2 : -1),
      { displayMode: display, throwOnError: false })
  })
}

const QUICK_PROMPTS = [
  { label: '总结论文', prompt: '请总结这篇论文：研究问题、方法、关键结果与结论。' },
  { label: '梳理论文大纲', prompt: '请按章节梳理这篇论文的大纲，每部分一两句话。' },
  { label: '生成学习指南', prompt: '请为这篇论文生成一份学习指南：需要先了解的背景知识、核心概念解释、建议的阅读顺序。' },
  { label: '常见问题解答', prompt: '请列出读者对这篇论文最常问的问题并逐一解答（FAQ 形式）。' },
]

const messages = ref<ChatMessage[]>([])
const input = ref('')
const sending = ref(false)
const listRef = ref<HTMLElement | null>(null)
/** 历史会话：打开时从服务端恢复，默认折叠在顶部，点击展开 / 滚轮上滑回看 */
const historyCount = ref(0)
const historyOpen = ref(false)

onMounted(async () => {
  try {
    const { messages: history } = await api.getChatHistory(props.taskId)
    if (history.length) {
      messages.value = history
      historyCount.value = history.length
    }
  } catch { /* 历史读取失败不阻塞 */ }
})

function persist() {
  // 每次问答后整体保存；失败只记日志不打断阅读
  api.saveChatHistory(props.taskId, messages.value)
    .catch(e => console.warn('会话保存失败', e))
}

async function scrollBottom() {
  await nextTick()
  listRef.value?.scrollTo({ top: listRef.value.scrollHeight })
}

async function send(text: string) {
  const content = text.trim()
  if (!content || sending.value) return
  messages.value.push({ role: 'user', content })
  input.value = ''
  sending.value = true
  await scrollBottom()
  try {
    const { reply } = await api.chat(props.taskId, messages.value)
    messages.value.push({ role: 'assistant', content: reply })
    persist()
  } catch (e) {
    messages.value.pop()  // 失败则撤掉这条提问，避免错位
    ElMessage.error(`AI 助手调用失败：${(e as Error).message}`)
  } finally {
    sending.value = false
    await scrollBottom()
  }
}

function sendInput() {
  send(input.value)
}

function clearSession() {
  messages.value = []
  historyCount.value = 0
  historyOpen.value = false
  persist()
}

async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content)
    ElMessage.success('已复制')
  } catch {
    ElMessage.error('复制失败')
  }
}
</script>

<template>
  <aside class="ai-panel">
    <div class="ai-header">
      <span class="ai-title">阅读助手</span>
      <span style="flex:1" />
      <el-button size="small" text :disabled="!messages.length" @click="clearSession">
        清空会话
      </el-button>
      <el-button size="small" text @click="emit('close')">收起</el-button>
    </div>

    <div ref="listRef" class="ai-messages">
      <div v-if="!messages.length" class="ai-welcome">
        <p class="ai-hint">Hi，准备好阅读了吗？可以从下面的快速开始模板问起：</p>
        <div class="ai-quick">
          <div class="ai-quick-title">快速开始</div>
          <el-button
            v-for="q in QUICK_PROMPTS"
            :key="q.label"
            class="ai-quick-btn"
            size="small"
            :disabled="sending"
            @click="send(q.prompt)"
          >
            {{ q.label }}
          </el-button>
        </div>
      </div>

      <!-- 历史会话折叠条：此前的对话收在上面，点开或滚轮上滑即可回看 -->
      <div
        v-if="historyCount > 0 && !historyOpen"
        class="ai-history-divider"
        @click="historyOpen = true"
      >
        ▾ 此前的 {{ historyCount }} 条对话（点击展开）
      </div>
      <div
        v-else-if="historyCount > 0"
        class="ai-history-divider open"
        @click="historyOpen = false"
      >
        ▴ 收起此前的 {{ historyCount }} 条对话
      </div>

      <template
        v-for="(m, i) in messages"
        :key="i"
      >
        <div
          v-if="i >= historyCount || historyOpen"
          class="ai-msg"
          :class="[m.role, { 'is-history': i < historyCount }]"
        >
        <div v-if="m.role === 'assistant'" class="ai-bubble md" v-html="renderMd(m.content)" />
        <div v-else class="ai-bubble">{{ m.content }}</div>
        <el-button
          v-if="m.role === 'assistant'"
          size="small"
          text
          class="ai-copy"
          @click="copyMessage(m.content)"
        >
          复制
        </el-button>
        </div>
      </template>
      <div v-if="sending" class="ai-msg assistant">
        <div class="ai-bubble ai-thinking">正在思考…</div>
      </div>
    </div>

    <div class="ai-input">
      <el-input
        v-model="input"
        type="textarea"
        :rows="2"
        resize="none"
        placeholder="就这篇论文提问…"
        @keydown.enter.exact.prevent="sendInput"
      />
      <div class="ai-input-bar">
        <span class="ai-disclaimer">内容由 AI 生成，仅供参考</span>
        <el-button
          type="primary"
          size="small"
          :loading="sending"
          :disabled="!input.trim()"
          @click="sendInput"
        >
          发送
        </el-button>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.ai-panel {
  width: 340px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--r-panel, #1a1b1f);
  border-right: 1px solid var(--r-border, #26282e);
  transition: background .25s, border-color .25s;
}
.ai-header {
  height: 44px;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 0 8px 0 16px;
  border-bottom: 1px solid var(--r-border, #26282e);
  flex-shrink: 0;
}
.ai-title {
  font-weight: 600;
  font-size: 14px;
  color: var(--r-text, #d7dae0);
  letter-spacing: 1px;
}
.ai-header :deep(.el-button) {
  color: var(--r-soft, #8b8f98);
}
.ai-header :deep(.el-button:hover) {
  color: var(--r-accent-text, #cfe3f7);
}
.ai-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px 14px;
  min-height: 0;
  scrollbar-width: thin;
  scrollbar-color: var(--r-chip, #33363d) transparent;
}
.ai-messages::-webkit-scrollbar {
  width: 6px;
}
.ai-messages::-webkit-scrollbar-thumb {
  background: var(--r-chip, #33363d);
  border-radius: 3px;
}
.ai-hint {
  font-size: 13px;
  color: var(--r-soft, #a8adb7);
  margin: 0 0 14px;
  line-height: 1.7;
}
.ai-quick-title {
  font-size: 12px;
  color: var(--r-muted, #5c616b);
  margin-bottom: 10px;
  letter-spacing: 1px;
}
.ai-quick {
  display: flex;
  flex-direction: column;
  align-items: stretch;
}
.ai-quick-btn {
  margin: 0 0 8px;
  justify-content: flex-start;
  background: transparent;
  border-color: var(--r-chip, #33363d);
  color: var(--r-soft, #b9bec8);
  border-radius: 8px;
  transition: all .25s;
}
.ai-quick-btn:hover {
  border-color: var(--r-accent, #4a90d9);
  color: var(--r-accent-text, #cfe3f7);
  background: var(--r-bubble, rgba(74, 144, 217, 0.08));
  transform: translateX(3px);
}
.ai-msg {
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  animation: msg-in .35s cubic-bezier(.22, .8, .32, 1) both;
}
@keyframes msg-in {
  from {
    opacity: 0;
    transform: translateY(10px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.ai-msg.user {
  align-items: flex-end;
}
.ai-bubble {
  max-width: 100%;
  padding: 10px 13px;
  border-radius: 10px;
  font-size: 13.5px;
  line-height: 1.8;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--r-bubble, #26282e);
  color: var(--r-text, #e2e4e9);
}
/* 历史会话折叠条 */
.ai-history-divider {
  text-align: center;
  font-size: 12px;
  color: var(--r-muted, #5c616b);
  padding: 6px 0;
  margin-bottom: 12px;
  border-top: 1px dashed var(--r-chip, #33363d);
  border-bottom: 1px dashed var(--r-chip, #33363d);
  cursor: pointer;
  transition: color .2s, border-color .2s;
  user-select: none;
}
.ai-history-divider:hover {
  color: var(--r-accent-text, #cfe3f7);
  border-color: var(--r-accent, #4a90d9);
}
.ai-msg.is-history {
  opacity: .72;
}

/* Markdown 气泡：交给渲染器排版，不用 pre-wrap */
.ai-bubble.md {
  white-space: normal;
}
.ai-bubble.md :deep(p) { margin: 0 0 .6em; }
.ai-bubble.md :deep(p:last-child) { margin-bottom: 0; }
.ai-bubble.md :deep(h1),
.ai-bubble.md :deep(h2),
.ai-bubble.md :deep(h3),
.ai-bubble.md :deep(h4) {
  margin: .8em 0 .4em;
  font-size: 1.05em;
  color: var(--r-heading, #f2f3f5);
}
.ai-bubble.md :deep(ul),
.ai-bubble.md :deep(ol) {
  margin: .3em 0 .6em;
  padding-left: 1.4em;
}
.ai-bubble.md :deep(li) { margin-bottom: .25em; }
.ai-bubble.md :deep(code) {
  background: var(--r-code-bg, #1a1c20);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: .92em;
}
.ai-bubble.md :deep(pre) {
  background: var(--r-code-bg, #1a1c20);
  border-radius: 8px;
  padding: 10px 12px;
  overflow-x: auto;
  margin: .5em 0;
}
.ai-bubble.md :deep(pre code) {
  background: none;
  padding: 0;
}
.ai-bubble.md :deep(blockquote) {
  margin: .4em 0;
  padding: 2px 10px;
  border-left: 3px solid var(--r-accent, #4a90d9);
  color: var(--r-soft, #8b8f98);
}
.ai-bubble.md :deep(table) {
  border-collapse: collapse;
  margin: .5em 0;
  font-size: .92em;
}
.ai-bubble.md :deep(th),
.ai-bubble.md :deep(td) {
  border: 1px solid var(--r-chip, #33363d);
  padding: 4px 8px;
}
.ai-bubble.md :deep(a) { color: var(--r-accent, #4a90d9); }
.ai-bubble.md :deep(.katex) { color: inherit; font-size: 1.03em; }
.ai-msg.user .ai-bubble {
  background: var(--r-bubble-user, #2b4a6b);
  color: var(--r-accent-text, #e8f1fa);
}
.ai-thinking {
  color: var(--r-muted, #5c616b);
  animation: breathe 1.4s ease-in-out infinite;
}
@keyframes breathe {
  0%, 100% { opacity: .45; }
  50% { opacity: 1; }
}
.ai-copy {
  padding: 0 4px;
  height: 22px;
  color: var(--r-muted, #5c616b);
}
.ai-copy:hover {
  color: var(--r-accent-text, #cfe3f7);
}
.ai-input {
  border-top: 1px solid var(--r-border, #26282e);
  padding: 12px;
  flex-shrink: 0;
}
.ai-input :deep(.el-textarea__inner) {
  background: var(--r-bubble, #26282e);
  border-color: var(--r-chip, #33363d);
  color: var(--r-text, #e2e4e9);
  box-shadow: none;
  transition: border-color .25s;
}
.ai-input :deep(.el-textarea__inner:focus) {
  border-color: var(--r-accent, #4a90d9);
}
.ai-input-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
}
.ai-disclaimer {
  font-size: 11px;
  color: var(--r-muted, #4a4e56);
}
</style>
