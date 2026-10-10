<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
/** 阅读页是独立全屏界面，不显示任务/配置侧边栏 */
const isReader = computed(() => route.path.startsWith('/reader'))

const asset = ref<{ ready: boolean | null; warming: boolean; message: string }>({
  ready: null, warming: false, message: '检查中…',
})

async function checkAssets() {
  try {
    asset.value = await fetch('/api/assets/status').then(r => r.json())
  } catch { asset.value.message = '后端未连接' }
}

function warmup() {
  asset.value.warming = true
  fetch('/api/assets/warmup', { method: 'POST' })
    .then(() => setTimeout(checkAssets, 1500))
}

onMounted(checkAssets)
</script>

<template>
  <div class="layout" :class="{ 'reader-mode': isReader }">
    <aside v-if="!isReader" class="sidebar">
      <div class="brand">译阅</div>
      <el-menu :default-active="$route.path" router>
        <el-menu-item index="/">翻译任务</el-menu-item>
        <el-menu-item index="/archive">归档</el-menu-item>
        <el-menu-item index="/settings">API 配置</el-menu-item>
        <el-menu-item index="/glossary">术语表</el-menu-item>
      </el-menu>
      <div class="asset-state">
        <template v-if="asset.ready === true">
          <el-text type="success" size="small">● 引擎资产就绪</el-text>
        </template>
        <template v-else-if="asset.warming">
          <el-text type="warning" size="small">正在准备引擎资产…</el-text>
        </template>
        <template v-else>
          <el-text type="danger" size="small">● 引擎资产未就绪</el-text>
          <el-button size="small" text type="primary" @click="warmup">
            立即下载
          </el-button>
        </template>
      </div>
    </aside>
    <main class="main">
      <router-view @assets-changed="checkAssets" />
    </main>
  </div>
</template>
