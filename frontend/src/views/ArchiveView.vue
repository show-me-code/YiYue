<script setup lang="ts">
/** 归档页：已归档论文的检索与主题筛选。 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api, type Task } from '../api/client'
import { fmtLocalTime } from '../utils/time'

const router = useRouter()
const tasks = ref<Task[]>([])
const keyword = ref('')
const activeTopic = ref('')

onMounted(async () => {
  try {
    tasks.value = (await api.listTasks()).filter(t => t.archived)
  } catch (e) {
    ElMessage.error((e as Error).message)
  }
})

/** 全部主题词（按出现频次排序） */
const allTopics = computed(() => {
  const count = new Map<string, number>()
  for (const t of tasks.value) {
    for (const tp of t.topics) count.set(tp, (count.get(tp) ?? 0) + 1)
  }
  return [...count.entries()].sort((a, b) => b[1] - a[1]).map(([tp]) => tp)
})

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return tasks.value.filter(t => {
    if (activeTopic.value && !t.topics.includes(activeTopic.value)) return false
    if (!kw) return true
    return [t.title_zh, t.title_en, t.filename, ...t.topics]
      .filter(Boolean)
      .some(s => s!.toLowerCase().includes(kw))
  })
})

function openReader(t: Task) { router.push(`/reader/${t.id}`) }

async function unarchive(t: Task) {
  await api.archiveTask(t.id, false)
  tasks.value = tasks.value.filter(x => x.id !== t.id)
  ElMessage.success('已移出归档')
}

function displayTitle(t: Task): string {
  return t.title_zh || t.title_en || t.filename
}
</script>

<template>
  <div class="page">
    <div class="card">
      <h3 style="margin-top:0">归档 · 已完成的论文</h3>
      <div class="archive-bar">
        <el-input
          v-model="keyword"
          placeholder="搜索标题 / 主题 / 文件名…"
          clearable
          style="max-width:360px"
        />
      </div>
      <div v-if="allTopics.length" class="topic-row">
        <el-check-tag
          :checked="!activeTopic"
          style="margin-right:8px"
          @click="activeTopic = ''"
        >全部</el-check-tag>
        <el-check-tag
          v-for="tp in allTopics"
          :key="tp"
          :checked="activeTopic === tp"
          style="margin-right:8px;margin-bottom:6px"
          @click="activeTopic = activeTopic === tp ? '' : tp"
        >{{ tp }}</el-check-tag>
      </div>
    </div>

    <el-empty v-if="!filtered.length" description="暂无归档的论文" />

    <div v-for="t in filtered" :key="t.id" class="card archive-card">
      <div class="archive-main" @click="openReader(t)">
        <b>{{ displayTitle(t) }}</b>
        <div v-if="t.title_zh && t.title_en" class="muted title-sub">{{ t.title_en }}</div>
        <div class="muted archive-meta">
          {{ t.page_count }} 页 · 完成于 {{ fmtLocalTime(t.finished_at) }}
          <el-tag v-if="t.arxiv_id" size="small" type="warning" style="margin-left:6px">
            arXiv {{ t.arxiv_id }}
          </el-tag>
        </div>
      </div>
      <div class="archive-topics">
        <el-tag v-for="tp in t.topics" :key="tp" size="small" effect="plain" type="info">
          {{ tp }}
        </el-tag>
      </div>
      <div class="archive-actions">
        <el-button size="small" type="primary" @click="openReader(t)">阅读</el-button>
        <el-button size="small" text @click="unarchive(t)">移出归档</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.archive-bar {
  margin-bottom: 10px;
}
.topic-row {
  display: flex;
  flex-wrap: wrap;
}
.archive-card {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}
.archive-main {
  flex: 1;
  min-width: 260px;
  cursor: pointer;
}
.archive-main:hover b {
  color: #409eff;
}
.title-sub {
  font-size: 12px;
  margin-top: 2px;
}
.archive-meta {
  margin-top: 4px;
  font-size: 12px;
}
.archive-topics {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.archive-actions {
  display: flex;
  gap: 4px;
}
</style>
