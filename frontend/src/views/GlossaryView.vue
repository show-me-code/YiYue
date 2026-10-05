<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type GlossaryEntry } from '../api/client'

const entries = ref<GlossaryEntry[]>([])
const saving = ref(false)

const form = reactive({ source: '', target: '', tgt_lang: 'zh', enabled: true })

async function refresh() {
  entries.value = await api.listGlossary().catch(() => [])
}
onMounted(refresh)

async function add() {
  if (!form.source.trim() || !form.target.trim()) {
    ElMessage.warning('原文与译文都要填写')
    return
  }
  saving.value = true
  try {
    await api.createGlossary({ ...form })
    form.source = ''
    form.target = ''
    await refresh()
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

async function toggle(e: GlossaryEntry) {
  await api.updateGlossary(e.id, {
    source: e.source, target: e.target, tgt_lang: e.tgt_lang, enabled: e.enabled,
  })
}

async function remove(e: GlossaryEntry) {
  await api.deleteGlossary(e.id)
  refresh()
}

async function importCsv(file: File) {
  const fd = new FormData()
  fd.append('file', file)
  const r = await fetch('/api/glossary/import', { method: 'POST', body: fd })
  if (r.ok) {
    const body = await r.json()
    ElMessage.success(`导入 ${body.imported} 条`)
    refresh()
  } else {
    const body = await r.json().catch(() => ({}))
    ElMessage.error(body.detail ?? `导入失败（HTTP ${r.status}）`)
  }
  return false
}

function exportCsv() {
  window.open('/api/glossary/export', '_blank')
}
</script>

<template>
  <div class="page">
    <div class="card">
      <div style="display:flex;align-items:center">
        <h3 style="margin:0;flex:1">术语表</h3>
        <el-upload :auto-upload="false" :show-file-list="false" accept=".csv"
          :on-change="(f: any) => importCsv(f.raw)">
          <el-button size="small">导入 CSV</el-button>
        </el-upload>
        <el-button size="small" style="margin-left:8px" @click="exportCsv">导出 CSV</el-button>
      </div>
      <p class="muted" style="margin:8px 0 0">
        翻译时启用的条目会注入 LLM 提示词，保证专业名词译法一致。目标语言留空表示对任何目标语言生效。
      </p>
      <el-form inline style="margin-top:12px">
        <el-form-item><el-input v-model="form.source" placeholder="原文术语" style="width:200px" /></el-form-item>
        <el-form-item><el-input v-model="form.target" placeholder="固定译法" style="width:200px" /></el-form-item>
        <el-form-item>
          <el-select v-model="form.tgt_lang" style="width:140px">
            <el-option label="中文（zh）" value="zh" />
            <el-option label="英语（en）" value="en" />
            <el-option label="全部语言" value="" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="saving" @click="add">添加</el-button>
        </el-form-item>
      </el-form>
    </div>

    <el-table :data="entries" style="width:100%">
      <el-table-column prop="source" label="原文" />
      <el-table-column prop="target" label="译法" />
      <el-table-column prop="tgt_lang" label="目标语言" width="110">
        <template #default="{ row }">{{ row.tgt_lang || '全部' }}</template>
      </el-table-column>
      <el-table-column label="启用" width="90">
        <template #default="{ row }">
          <el-switch v-model="row.enabled" @change="toggle(row)" />
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button size="small" type="danger" text @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>
