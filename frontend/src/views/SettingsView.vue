<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type Profile, type ProfileInput } from '../api/client'

const profiles = ref<Profile[]>([])
const dialogVisible = ref(false)
const editingId = ref<number | null>(null)
const testing = ref<number | null>(null)
const saving = ref(false)

const form = reactive<ProfileInput>({
  name: '', base_url: '', api_key: '', model: 'gpt-4o-mini',
  qps: 4, pool_max_workers: null, custom_system_prompt: null, is_default: false,
})

async function refresh() {
  profiles.value = await api.listProfiles().catch(() => [])
}
onMounted(refresh)

function openCreate() {
  editingId.value = null
  Object.assign(form, {
    name: '', base_url: '', api_key: '', model: 'gpt-4o-mini',
    qps: 4, pool_max_workers: null, custom_system_prompt: null, is_default: false,
  })
  dialogVisible.value = true
}

function openEdit(p: Profile) {
  editingId.value = p.id
  Object.assign(form, {
    name: p.name, base_url: p.base_url, api_key: '', model: p.model,
    qps: p.qps, pool_max_workers: p.pool_max_workers,
    custom_system_prompt: p.custom_system_prompt, is_default: p.is_default,
  })
  dialogVisible.value = true
}

async function save() {
  saving.value = true
  try {
    if (editingId.value) await api.updateProfile(editingId.value, { ...form })
    else await api.createProfile({ ...form })
    dialogVisible.value = false
    await refresh()
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    saving.value = false
  }
}

async function test(p: Profile) {
  testing.value = p.id
  try {
    const r = await api.testProfile(p.id)
    if (r.ok) ElMessage.success(r.detail)
    else ElMessage.error(r.detail)
  } finally {
    testing.value = null
  }
}

async function remove(p: Profile) {
  await api.deleteProfile(p.id)
  refresh()
}
</script>

<template>
  <div class="page">
    <div class="card">
      <div style="display:flex;align-items:center">
        <h3 style="margin:0;flex:1">API 配置档</h3>
        <el-button type="primary" @click="openCreate">新增配置</el-button>
      </div>
      <p class="muted" style="margin:8px 0 0">
        兼容一切 OpenAI 格式接口：OpenAI / DeepSeek / GLM / SiliconFlow / one-api 中转 /
        Ollama、LM Studio 等本地服务（本地服务 API Key 可留空）。
      </p>
    </div>

    <div class="card" v-for="p in profiles" :key="p.id">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <b>{{ p.name }}</b>
        <el-tag v-if="p.is_default" size="small">默认</el-tag>
        <span class="muted">{{ p.model }}</span>
        <span class="muted">{{ p.base_url || '官方 OpenAI 端点' }}</span>
        <span class="muted">{{ p.api_key_masked || '无密钥' }}</span>
        <span class="muted">QPS {{ p.qps }}</span>
        <span style="flex:1" />
        <el-button size="small" :loading="testing === p.id" @click="test(p)">测试连接</el-button>
        <el-button size="small" @click="openEdit(p)">编辑</el-button>
        <el-button size="small" type="danger" text @click="remove(p)">删除</el-button>
      </div>
    </div>
    <div v-if="!profiles.length" class="card muted">还没有配置档，点击右上角「新增配置」开始。</div>

    <el-dialog v-model="dialogVisible" :title="editingId ? '编辑配置' : '新增配置'" width="560">
      <el-form label-width="130px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如：DeepSeek 官方 / 本地 Ollama" />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="form.base_url" placeholder="留空 = https://api.openai.com/v1" />
        </el-form-item>
        <el-form-item label="API Key">
          <el-input v-model="form.api_key" type="password" show-password
            :placeholder="editingId ? '留空则保持原 Key 不变' : '本地服务可留空'" />
        </el-form-item>
        <el-form-item label="模型名">
          <el-input v-model="form.model" placeholder="如 deepseek-chat / gpt-4o-mini / glm-4-flash" />
        </el-form-item>
        <el-form-item label="QPS 限制">
          <el-input-number v-model="form.qps" :min="1" :max="100" />
          <span class="muted" style="margin-left:8px">每秒最多请求数，防止 429</span>
        </el-form-item>
        <el-form-item label="自定义系统提示">
          <el-input v-model="form.custom_system_prompt" type="textarea" :rows="2"
            placeholder="可选；如 Qwen3 需要 /no_think 时使用" />
        </el-form-item>
        <el-form-item label="设为默认">
          <el-switch v-model="form.is_default" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>
