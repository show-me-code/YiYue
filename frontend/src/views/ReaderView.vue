<script setup lang="ts">
/** 阅读页：加载任务输出 + 订阅进度流，实现"翻完即读"。 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import DualReader from '../components/DualReader.vue'
import { api, openTaskEvents, type Task } from '../api/client'

const route = useRoute()
const taskId = Number(route.params.id)

const task = ref<Task | null>(null)
const pagesDone = ref(0)
const reloadKey = ref(0)
const mode = ref<'dual' | 'mono'>('dual')
const sync = ref(true)
let source: EventSource | null = null

const originalUrl = computed(() =>
  task.value?.outputs.find(o => o.kind === 'original')?.url ?? null)
const monoUrl = computed(() =>
  task.value?.outputs.find(o => o.kind === 'mono')?.url ?? null)
const dualUrl = computed(() =>
  task.value?.outputs.find(o => o.kind === 'dual')?.url ?? null)
const live = computed(() =>
  ['queued', 'translating', 'merging'].includes(task.value?.status ?? ''))

function applyTask(t: Task) {
  task.value = t
  const done = Math.max(t.progress.pages_done ?? 0, 0)
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

onMounted(async () => {
  try {
    applyTask(await api.getTask(taskId))
  } catch (e) {
    ElMessage.error((e as Error).message)
    return
  }
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
      api.getTask(taskId).then(applyTask).catch(() => {})
      reloadKey.value++
      ElMessage.success('翻译完成')
    } else if (type === 'error') {
      ElMessage.error(String(ev.message ?? '翻译失败'))
    }
  })
})

onBeforeUnmount(() => source?.close())
</script>

<template>
  <div class="page reader-page">
    <div v-if="task" class="reader-header">
      <span><b>{{ task.filename }}</b></span>
      <el-tag size="small" :type="live ? 'primary' : task.status === 'done' ? 'success' : 'info'">
        {{ live ? `翻译中 · ${task.progress.stage ?? ''}` : task.status }}
      </el-tag>
      <el-tag v-if="task.engine_used === 'arxiv'" size="small" type="success">
        arXiv 源码直译（LaTeX 重编译）
      </el-tag>
      <el-tag v-else-if="task.engine_used === 'babeldoc'" size="small" type="warning">
        原位排版（PDF 模式，未走 LaTeX）
      </el-tag>
      <span class="muted">{{ pagesDone }}/{{ task.page_count }} 页可读</span>
      <span style="flex:1" />
      <el-link v-if="monoUrl" :href="monoUrl" target="_blank">译文 PDF</el-link>
      <el-link v-if="dualUrl" :href="dualUrl" target="_blank" style="margin-left:12px">双语 PDF</el-link>
      <el-link v-if="originalUrl" :href="originalUrl" target="_blank" style="margin-left:12px">原文</el-link>
    </div>

    <div v-if="task?.status === 'done' && task?.error" class="reader-note">
      <el-alert :title="task.error" type="warning" :closable="false" show-icon />
    </div>

    <DualReader
      v-if="monoUrl"
      v-model:mode="mode"
      v-model:sync="sync"
      :original-url="originalUrl"
      :mono-url="monoUrl"
      :pages-done="pagesDone"
      :reload-key="reloadKey"
      :expected-total="task?.page_count ?? 0"
    />
    <div v-else class="reader-empty">
      <el-empty description="译文尚未生成，请等待任务开始输出" />
    </div>
  </div>
</template>

<style scoped>
.reader-header {
  height: 40px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  flex-shrink: 0;
}
.reader-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.reader-note {
  padding: 8px 14px 0;
}
.muted {
  color: #909399;
  font-size: 12px;
}
</style>
