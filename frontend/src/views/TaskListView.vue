<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { UploadFilled } from '@element-plus/icons-vue'
import { api, type Profile, type Task, type UploadOptions } from '../api/client'

const router = useRouter()

const profiles = ref<Profile[]>([])
const tasks = ref<Task[]>([])
const fileList = ref<File[]>([])
const submitting = ref(false)
let timer: number | undefined

const form = reactive<UploadOptions>({
  profile_id: 0,
  lang_in: 'en',
  lang_out: 'zh',
  pages: '',
  part_size: 20,
  scan_mode: 'off',
  auto_extract_glossary: false,
  use_glossary: true,
  engine: 'auto',
  arxiv_id: '',
})

const STATUS_META: Record<Task['status'], { label: string; type: 'info' | 'primary' | 'warning' | 'success' | 'danger' }> = {
  queued: { label: '排队中', type: 'info' },
  translating: { label: '翻译中', type: 'primary' },
  merging: { label: '合并中', type: 'warning' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'info' },
}

function fmtSize(n: number): string {
  return n > 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${(n / 1024).toFixed(0)} KB`
}

async function refresh() {
  try {
    tasks.value = await api.listTasks()
  } catch { /* 后端未就绪时静默 */ }
}

onMounted(async () => {
  profiles.value = await api.listProfiles().catch(() => [])
  if (profiles.value.length) {
    form.profile_id = (profiles.value.find(p => p.is_default) ?? profiles.value[0]).id
  }
  await refresh()
  timer = window.setInterval(refresh, 2500)
})
onBeforeUnmount(() => window.clearInterval(timer))

function onFileChange(files: File[]) {
  fileList.value = files.slice(0, 1)
}

async function submit() {
  if (!fileList.value.length && !form.arxiv_id.trim()) {
    ElMessage.warning('请上传 PDF 文件，或填写 arXiv ID（二选一即可）')
    return
  }
  if (!form.profile_id) { ElMessage.warning('请先在「API 配置」中添加配置档'); return }
  submitting.value = true
  try {
    const task = await api.createTask(fileList.value[0] ?? null, { ...form })
    ElMessage.success(task.status === 'done' ? '缓存命中，任务已完成' : '任务已创建')
    fileList.value = []
    await refresh()
  } catch (e) {
    ElMessage.error(`上传失败：${(e as Error).message}`)
  } finally {
    submitting.value = false
  }
}

async function cancel(t: Task) { await api.cancelTask(t.id); refresh() }
async function retry(t: Task) { await api.retryTask(t.id); refresh() }
async function remove(t: Task) {
  await api.deleteTask(t.id)
  refresh()
}
function openReader(t: Task) { router.push(`/reader/${t.id}`) }

function progressPct(t: Task): number {
  if (t.status === 'done') return 100
  return Math.min(100, Math.round(t.progress.overall_progress ?? 0))
}
</script>

<template>
  <div class="page">
    <div class="card">
      <h3 style="margin-top:0">新建翻译任务</h3>
      <el-upload
        drag
        :auto-upload="false"
        :limit="1"
        :on-change="(f: any) => onFileChange([f.raw])"
        :on-remove="() => onFileChange([])"
        :show-file-list="true"
        accept="application/pdf"
      >
        <el-icon style="font-size:40px;color:#c0c4cc"><UploadFilled /></el-icon>
        <div>拖拽 PDF 到此处（可留空，改填 arXiv ID）</div>
        <template #tip>
          <div class="muted">
            支持 PDF；填写 arXiv ID 时可不上传文件，服务端自动下载源码与原文。
            识别到 arXiv ID 的论文优先走「LaTeX 源码直译重编译」。
          </div>
        </template>
      </el-upload>

      <el-form label-width="110px" style="margin-top:12px;max-width:720px">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="API 配置">
              <el-select v-model="form.profile_id" style="width:100%">
                <el-option v-for="p in profiles" :key="p.id"
                  :label="`${p.name}（${p.model}）`" :value="p.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="源语言">
              <el-select v-model="form.lang_in" style="width:100%">
                <el-option label="英语" value="en" />
                <el-option label="日语" value="ja" />
                <el-option label="韩语" value="ko" />
                <el-option label="中文" value="zh" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="目标语言">
              <el-select v-model="form.lang_out" style="width:100%">
                <el-option label="简体中文" value="zh" />
                <el-option label="繁体中文" value="zt" />
                <el-option label="英语" value="en" />
                <el-option label="日语" value="ja" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="翻译引擎">
              <el-radio-group v-model="form.engine">
                <el-radio-button value="auto">自动</el-radio-button>
                <el-radio-button value="arxiv">arXiv 直译</el-radio-button>
                <el-radio-button value="babeldoc">原位排版</el-radio-button>
              </el-radio-group>
              <div class="muted">
                自动：识别到 arXiv ID 时用「源码直译重编译」（排版与原版一致），
                否则用原位排版引擎；失败自动回退。
              </div>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="arXiv ID">
              <el-input v-model="form.arxiv_id" placeholder="可选，如 1610.01644 或 arxiv.org/abs/… 链接" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="页码范围">
              <el-input v-model="form.pages" placeholder="留空=全部，如 1-5,8（arXiv 直译时忽略）" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="分段页数">
              <el-input-number v-model="form.part_size" :min="0" :max="200" style="width:100%" />
              <div class="muted">0 = 整本一次翻译；大文件按段“翻完即读”</div>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="扫描件模式">
              <el-select v-model="form.scan_mode" style="width:100%">
                <el-option label="关闭（默认）" value="off" />
                <el-option label="扫描件覆盖（实验）" value="workaround" />
                <el-option label="自动检测（实验）" value="auto" />
              </el-select>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="术语表">
          <el-switch v-model="form.use_glossary" />
          <span class="muted" style="margin-left:8px">应用「术语表」页面中的启用条目</span>
        </el-form-item>
        <el-form-item label="自动术语提取">
          <el-switch v-model="form.auto_extract_glossary" />
          <span class="muted" style="margin-left:8px">额外消耗 token，可自动抽取领域术语</span>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="submit">开始翻译</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div class="card" v-for="t in tasks" :key="t.id">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <el-tag :type="STATUS_META[t.status].type">{{ STATUS_META[t.status].label }}</el-tag>
        <b>{{ t.filename }}</b>
        <span class="muted">{{ t.page_count }} 页 · {{ fmtSize(t.size_bytes) }} · {{ t.lang_in }}→{{ t.lang_out }}</span>
        <el-tag v-if="t.cache_hit" size="small" type="success">缓存命中</el-tag>
        <el-tag v-if="t.arxiv_id" size="small" type="warning">arXiv {{ t.arxiv_id }}</el-tag>
        <el-tag v-if="t.engine_used === 'arxiv'" size="small" type="success">arXiv 源码直译</el-tag>
        <el-tag v-else-if="t.engine_used === 'babeldoc'" size="small" type="warning">
          原位排版（PDF 模式，未走 LaTeX）
        </el-tag>
        <el-tag v-else-if="t.engine === 'babeldoc'" size="small" type="info">原位排版</el-tag>
        <el-tag
          v-if="t.verification" size="small"
          :type="t.verification.verdict === '通过' ? 'success' : 'warning'"
        >闭环验证：{{ t.verification.verdict }}</el-tag>
        <span v-if="t.pages" class="muted">页码 {{ t.pages }}</span>
        <span style="flex:1" />
        <el-button v-if="t.status === 'done'" size="small" type="primary" @click="openReader(t)">对照阅读</el-button>
        <el-button v-if="['queued','translating','merging'].includes(t.status)" size="small" @click="cancel(t)">取消</el-button>
        <el-button v-if="['failed','cancelled','done'].includes(t.status)" size="small" @click="retry(t)">重试</el-button>
        <el-button v-if="t.status === 'done'" size="small">
          <a :href="t.outputs.find(o=>o.kind==='mono')?.url" target="_blank" style="text-decoration:none;color:inherit">译文 PDF</a>
        </el-button>
        <el-button v-if="t.status === 'done' && t.outputs.some(o=>o.kind==='dual')" size="small">
          <a :href="t.outputs.find(o=>o.kind==='dual')?.url" target="_blank" style="text-decoration:none;color:inherit">双语 PDF</a>
        </el-button>
        <el-button size="small" type="danger" text @click="remove(t)">删除</el-button>
      </div>

      <el-progress
        v-if="['queued','translating','merging'].includes(t.status)"
        :percentage="progressPct(t)" style="margin-top:10px"
      />
      <div v-if="['translating','merging'].includes(t.status)" class="muted" style="margin-top:4px">
        {{ t.progress.stage }} · 第 {{ (t.progress.part_index ?? 0) + 1 }}/{{ Math.max(t.progress.part_total ?? 1, 1) }} 段
        · 已可读 {{ t.progress.pages_done ?? 0 }}/{{ t.page_count }} 页
        <el-button v-if="(t.progress.pages_done ?? 0) > 0" link type="primary" size="small" @click="openReader(t)">立即阅读已完成部分</el-button>
      </div>
      <el-alert v-if="t.status === 'failed'" :title="t.error ?? '翻译失败'" type="error" :closable="false" style="margin-top:10px" />
      <el-alert
        v-else-if="t.status === 'done' && t.error"
        :title="t.error" type="warning" :closable="false" show-icon style="margin-top:10px"
      />
    </div>
  </div>
</template>
