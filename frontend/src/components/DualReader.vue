<script setup lang="ts">
/** 双栏对照阅读器：左原文右译文，按页码锚点同步滚动与缩放。 */
import { computed, ref, watch } from 'vue'
import PdfPane from './PdfPane.vue'

const props = defineProps<{
  originalUrl: string | null
  monoUrl: string | null
  /** 译文已可读页数（0 = 未开始） */
  pagesDone: number
  /** 变化时强制译文面板重载（增量合并后） */
  reloadKey: number
  /** 期望总页数（用于"翻译中"占位页） */
  expectedTotal: number
}>()

const mode = defineModel<'dual' | 'mono'>('mode', { default: 'dual' })
const syncEnabled = defineModel<boolean>('sync', { default: true })

const zoom = ref(1)
const leftPage = ref(1)
const rightPage = ref(1)
const leftTotal = ref(0)
const rightTotal = ref(0)
const syncing = ref(false)

const leftPane = ref<InstanceType<typeof PdfPane> | null>(null)
const rightPane = ref<InstanceType<typeof PdfPane> | null>(null)
/** reload 后要跳回的页；等 PdfPane 真正载入完成再跳，不用猜时间 */
let pendingRestore: number | null = null

const rightUrl = computed(() => props.monoUrl)
const displayPage = computed(() => Math.min(rightPage.value, Math.max(1, props.pagesDone)))

function fromLeft(n: number) {
  leftPage.value = n
  if (syncEnabled.value && mode.value === 'dual' && !syncing.value) {
    syncing.value = true
    rightPane.value?.goToPage(n)
    rightPage.value = n
    syncing.value = false
  }
}

function fromRight(n: number) {
  rightPage.value = n
  if (syncEnabled.value && mode.value === 'dual' && !syncing.value) {
    syncing.value = true
    leftPane.value?.goToPage(n)
    leftPage.value = n
    syncing.value = false
  }
}

function zoomIn() { zoom.value = Math.min(3, Math.round((zoom.value + 0.1) * 10) / 10) }
function zoomOut() { zoom.value = Math.max(0.4, Math.round((zoom.value - 0.1) * 10) / 10) }
function resetZoom() { zoom.value = 1 }

function onRightLoaded(n: number) {
  rightTotal.value = n
  if (pendingRestore !== null) {
    // 用 'loaded' 事件而不是固定 setTimeout(600)：合并大文件时 PDF 可能要几秒，
    // 固定延时既可能太早（节点还没建好，跳页失效）也可能太晚。
    const target = Math.min(pendingRestore, Math.max(1, n))
    pendingRestore = null
    rightPane.value?.goToPage(target)
  }
}

watch(() => props.reloadKey, () => {
  // 增量合并后重载译文文档，尽量停留在当前阅读页
  pendingRestore = rightPage.value
  rightPane.value?.reload()
})
</script>

<template>
  <div class="dual-reader">
    <div class="toolbar">
      <el-radio-group v-model="mode" size="small">
        <el-radio-button value="dual">双栏对照</el-radio-button>
        <el-radio-button value="mono">仅译文</el-radio-button>
      </el-radio-group>
      <el-divider direction="vertical" />
      <el-button size="small" circle @click="zoomOut">−</el-button>
      <span class="zoom-label">{{ Math.round(zoom * 100) }}%</span>
      <el-button size="small" circle @click="zoomIn">＋</el-button>
      <el-button size="small" text @click="resetZoom">适配宽度</el-button>
      <el-divider direction="vertical" />
      <el-switch v-model="syncEnabled" size="small" active-text="同步滚动" />
      <span style="flex:1" />
      <span class="muted" style="color:#cfd4dc">
        第 {{ displayPage }} 页 / 原文 {{ leftTotal }} 页 · 已译 {{ pagesDone }} 页
      </span>
    </div>

    <div class="panes" :class="{ single: mode === 'mono' }">
      <PdfPane
        v-if="mode === 'dual'"
        ref="leftPane"
        class="half"
        :url="originalUrl"
        :zoom="zoom"
        @page-change="fromLeft"
        @loaded="(n: number) => (leftTotal = n)"
      />
      <PdfPane
        ref="rightPane"
        class="half"
        :url="rightUrl"
        :zoom="zoom"
        :expected-total="expectedTotal"
        @page-change="fromRight"
        @loaded="onRightLoaded"
      />
    </div>
  </div>
</template>

<style scoped>
.dual-reader {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.toolbar {
  height: 44px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px;
  background: #2b2f33;
  color: #e5e7eb;
  flex-shrink: 0;
}
.zoom-label {
  font-size: 12px;
  width: 42px;
  text-align: center;
}
.panes {
  flex: 1;
  display: flex;
  min-height: 0;
}
.panes .half {
  flex: 1;
  border-right: 1px solid #3a3f44;
}
.panes.single .half {
  border-right: none;
}
.muted {
  font-size: 12px;
}
</style>
