<script setup lang="ts">
/** 阅读页：独立全屏沉浸式界面（不挂任务/配置侧边栏）。
 *  深色顶栏 + 左侧 AI 阅读助手 + 主区「双栏对照 / 逐段对照」。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import DualReader from '../components/DualReader.vue'
import ParagraphFlow from '../components/ParagraphFlow.vue'
import AiAssistant from '../components/AiAssistant.vue'
import { api, openTaskEvents, type Task } from '../api/client'
import { finishLabel, fmtDuration, fmtLocalTime } from '../utils/time'

const route = useRoute()
const router = useRouter()
const taskId = Number(route.params.id)

const task = ref<Task | null>(null)
const pagesDone = ref(0)
const reloadKey = ref(0)
const mode = ref<'dual' | 'mono'>('dual')
const sync = ref(true)
const view = ref<'pdf' | 'flow'>('flow')
const assistantOpen = ref(true)
/** 阅读页主题：默认深色，localStorage 持久化 */
const dark = ref(localStorage.getItem('yiyue-reader-theme') !== 'light')
let source: EventSource | null = null

function toggleTheme() {
  dark.value = !dark.value
  localStorage.setItem('yiyue-reader-theme', dark.value ? 'dark' : 'light')
}

const originalUrl = computed(() =>
  task.value?.outputs.find(o => o.kind === 'original')?.url ?? null)
const monoUrl = computed(() =>
  task.value?.outputs.find(o => o.kind === 'mono')?.url ?? null)
const dualUrl = computed(() =>
  task.value?.outputs.find(o => o.kind === 'dual')?.url ?? null)
const live = computed(() =>
  ['queued', 'translating', 'merging'].includes(task.value?.status ?? ''))

function applyTask(t: Task) {
  // **合并**而不是替换：SSE 的 state 事件带的是精简快照（不含 outputs/verification/
  // 时间字段），直接覆盖会把已加载的输出、引擎标签、完成时间一起抹掉。
  task.value = { ...(task.value ?? {}), ...t } as Task
  const done = Math.max(t.progress?.pages_done ?? 0, 0)
  if (done > pagesDone.value) {
    // 有新页可读 → 触发译文面板重载
    pagesDone.value = t.status === 'done' ? t.page_count : done
    if (t.status !== 'done') reloadKey.value++
  }
  if (t.status === 'done' && pagesDone.value === 0) {
    pagesDone.value = t.page_count
    reloadKey.value++
  }
}

function closeEvents() {
  source?.close()
  source = null
}

onMounted(async () => {
  try {
    applyTask(await api.getTask(taskId))
  } catch (e) {
    ElMessage.error((e as Error).message)
    return
  }
  if (!live.value) return  // 任务已结束（读历史任务）就不用开事件流
  source = openTaskEvents(taskId, (ev) => {
    const type = ev.type as string
    if (type === 'state' && ev.task) applyTask(ev.task as Task)
    else if (type === 'progress') {
      const p = ev as { pages_done?: number; stage?: string }
      if (task.value && (p.pages_done ?? 0) >= 0) {
        task.value = { ...task.value, progress: { ...task.value.progress, ...p } }
      }
    } else if (type === 'pages_available') {
      pagesDone.value = Number(ev.pages_done ?? 0)
      reloadKey.value++
    } else if (type === 'done') {
      // 终态必须主动关闭：服务端生成器到此结束，EventSource 会不停自动重连，
      // 每次重连都白占一条 SSE 连接 + 一个订阅队列。
      closeEvents()
      api.getTask(taskId).then(applyTask).catch(() => {})
      reloadKey.value++
      ElMessage.success('翻译完成')
    } else if (type === 'cancelled') {
      closeEvents()
      api.getTask(taskId).then(applyTask).catch(() => {})
      ElMessage.info('任务已取消')
    } else if (type === 'error') {
      closeEvents()
      api.getTask(taskId).then(applyTask).catch(() => {})
      ElMessage.error(String(ev.message ?? '翻译失败'))
    }
  })
  source.onerror = () => {
    // 断线且任务已不在进行中：不再让浏览器自动重连
    if (!live.value) closeEvents()
  }
})

onBeforeUnmount(closeEvents)
</script>

<template>
  <div class="page reader-page" :class="{ light: !dark }">
    <div v-if="task" class="reader-header">
      <span class="back" @click="router.push('/')">‹ 返回</span>
      <span class="fname" :title="task.filename"><b>{{ task.title_zh || task.title_en || task.filename }}</b></span>
      <span class="chip" :class="live ? 'chip-live' : task.status === 'done' ? 'chip-ok' : ''">
        {{ live ? `翻译中 · ${task.progress.stage ?? ''}` : task.status }}
      </span>
      <span v-if="task.engine_used === 'arxiv'" class="chip chip-ok">arXiv 源码直译</span>
      <span v-else-if="task.engine_used === 'babeldoc'" class="chip chip-warn">原位排版</span>
      <span class="muted">{{ pagesDone }}/{{ task.page_count }} 页</span>
      <span
        v-if="finishLabel(task.status)"
        class="muted"
        :title="`创建于 ${fmtLocalTime(task.created_at)}`"
      >
        {{ finishLabel(task.status) }} {{ fmtLocalTime(task.finished_at) }}
        <template v-if="fmtDuration(task.duration_s)">· 耗时 {{ fmtDuration(task.duration_s) }}</template>
      </span>
      <span style="flex:1" />
      <el-radio-group v-model="view" size="small" class="view-switch">
        <el-radio-button value="flow">逐段对照</el-radio-button>
        <el-radio-button value="pdf">双栏对照</el-radio-button>
      </el-radio-group>
      <span
        class="ai-toggle"
        :class="{ on: assistantOpen }"
        @click="assistantOpen = !assistantOpen"
      >
        <span class="dot" />AI 助手
      </span>
      <span class="theme-toggle" :title="dark ? '切换为浅色' : '切换为深色'" @click="toggleTheme">
        {{ dark ? '☾' : '☀' }}
      </span>
      <a v-if="monoUrl" class="dl" :href="monoUrl" target="_blank">译文 PDF</a>
      <a v-if="dualUrl" class="dl" :href="dualUrl" target="_blank">双语 PDF</a>
      <a v-if="originalUrl" class="dl" :href="originalUrl" target="_blank">原文</a>
    </div>

    <div v-if="task?.status === 'done' && task?.error" class="reader-note">
      <el-alert :title="task.error" type="warning" :closable="false" show-icon dark />
    </div>

    <div class="reader-body">
      <AiAssistant
        v-if="assistantOpen"
        :task-id="taskId"
        @close="assistantOpen = false"
      />
      <DualReader
        v-if="view === 'pdf' && monoUrl"
        v-model:mode="mode"
        v-model:sync="sync"
        :original-url="originalUrl"
        :mono-url="monoUrl"
        :pages-done="pagesDone"
        :reload-key="reloadKey"
        :expected-total="task?.page_count ?? 0"
      />
      <ParagraphFlow
        v-else-if="view === 'flow'"
        :task-id="taskId"
        :reload-key="reloadKey"
        :live="live"
      />
      <div v-else class="reader-empty">
        <el-empty description="译文尚未生成，请等待任务开始输出" />
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 阅读页主题变量：默认深色，.light 覆盖为浅色。子组件统一用 var(--r-*) */
.reader-page {
  --r-bg: #141518;
  --r-panel: #1a1b1f;
  --r-border: #26282e;
  --r-text: #e9ebf0;
  --r-src: #9ba1ab;
  --r-src-hover: #b7bdc7;
  --r-heading: #f2f3f5;
  --r-muted: #5c616b;
  --r-soft: #8b8f98;
  --r-chip: #33363d;
  --r-accent: #4a90d9;
  --r-accent-text: #cfe3f7;
  --r-accent-bg: #2b4a6b;
  --r-bubble: #26282e;
  --r-bubble-user: #2b4a6b;
  --r-code-bg: #1a1c20;
  --r-dots: #3d4149;
  background: var(--r-bg);
  color: var(--r-text);
}
.reader-page.light {
  --r-bg: #f5f6f8;
  --r-panel: #ffffff;
  --r-border: #e4e7ed;
  --r-text: #1f2329;
  --r-src: #6b7280;
  --r-src-hover: #4b5563;
  --r-heading: #111418;
  --r-muted: #909399;
  --r-soft: #606266;
  --r-chip: #dcdfe6;
  --r-accent: #2f6fbd;
  --r-accent-text: #1d4e8a;
  --r-accent-bg: #dceaff;
  --r-bubble: #f4f4f5;
  --r-bubble-user: #dceaff;
  --r-code-bg: #f6f7f9;
  --r-dots: #d0d3d9;
}

/* ---------- 顶栏 ---------- */
.reader-header {
  height: 46px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 16px;
  background: var(--r-panel);
  border-bottom: 1px solid var(--r-border);
  flex-shrink: 0;
  color: var(--r-text);
  transition: background .25s, border-color .25s;
}
.back {
  cursor: pointer;
  color: var(--r-soft);
  font-size: 13px;
  padding: 4px 8px;
  border-radius: 6px;
  transition: color .2s, background .2s;
  user-select: none;
  white-space: nowrap;
}
.back:hover {
  color: var(--r-text);
  background: var(--r-bubble);
}
.fname {
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13.5px;
}
.chip {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--r-chip);
  color: var(--r-soft);
  white-space: nowrap;
}
.chip-live {
  border-color: var(--r-accent);
  color: var(--r-accent);
}
.chip-ok {
  border-color: #3f7a4e;
  color: #51a566;
}
.chip-warn {
  border-color: #8a6d3b;
  color: #b8860b;
}
.muted {
  color: var(--r-muted);
  font-size: 12px;
  white-space: nowrap;
}

/* 视图切换：药丸组 */
.view-switch :deep(.el-radio-button__inner) {
  background: transparent;
  border-color: var(--r-chip);
  color: var(--r-soft);
  transition: all .2s;
}
.view-switch :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner) {
  background: var(--r-accent-bg);
  border-color: var(--r-accent);
  color: var(--r-accent-text);
  box-shadow: none;
}

/* AI 助手开关 */
.ai-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--r-muted);
  cursor: pointer;
  user-select: none;
  padding: 4px 10px;
  border: 1px solid var(--r-chip);
  border-radius: 999px;
  transition: all .25s;
  white-space: nowrap;
}
.ai-toggle:hover {
  border-color: var(--r-accent);
  color: var(--r-accent-text);
}
.ai-toggle .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--r-chip);
  transition: background .25s;
}
.ai-toggle.on {
  color: var(--r-accent-text);
  border-color: var(--r-accent);
}
.ai-toggle.on .dot {
  background: var(--r-accent);
}

/* 主题切换 */
.theme-toggle {
  cursor: pointer;
  user-select: none;
  font-size: 15px;
  line-height: 1;
  width: 28px;
  height: 28px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--r-chip);
  border-radius: 999px;
  color: var(--r-soft);
  transition: all .25s;
}
.theme-toggle:hover {
  border-color: var(--r-accent);
  color: var(--r-accent-text);
  transform: rotate(20deg);
}

.dl {
  font-size: 12px;
  color: var(--r-soft);
  text-decoration: none;
  white-space: nowrap;
  transition: color .2s;
}
.dl:hover {
  color: var(--r-accent);
}

.reader-note {
  padding: 8px 14px 0;
}
.reader-body {
  flex: 1;
  display: flex;
  min-height: 0;
}
.reader-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.reader-empty :deep(.el-empty__description p) {
  color: var(--r-muted);
}
</style>
