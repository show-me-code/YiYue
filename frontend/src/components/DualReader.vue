<script setup lang="ts">
/** 双栏对照阅读器：左原文右译文，按比例同步滚动与缩放。 */
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

const leftPane = ref<InstanceType<typeof PdfPane> | null>(null)
const rightPane = ref<InstanceType<typeof PdfPane> | null>(null)
/** reload 后要跳回的页；等 PdfPane 真正载入完成再跳，不用猜时间 */
let pendingRestore: number | null = null

const rightUrl = computed(() => props.monoUrl)
const displayPage = computed(() => Math.min(rightPage.value, Math.max(1, props.pagesDone)))

/**
 * 同步滚动：页锚点对齐——左栏"第 N 页的 40% 处"映射到右栏对应页的 40% 处
 * （页码按两边总页数换算：17 页原文的第 9 页 ≈ 19 页译文的第 10 页）。
 * 比纯滚动比例更稳：两栏翻页节奏一致，速度感受相同。
 * 抑制窗口：程序化设置对方 scrollTop 会异步触发对方的 scroll 事件，
 * 若不加窗（旧实现），回弹事件被当成用户滚动又同步回来，两栏互相拉扯乱跳。
 */
const suppressUntil = { left: 0, right: 0 }

function syncFrom(side: 'left' | 'right', pos: { page: number; ratio: number }) {
  if (!syncEnabled.value || mode.value !== 'dual') return
  if (Date.now() < suppressUntil[side]) return
  const fromTotal = side === 'left' ? leftTotal.value : rightTotal.value
  const toTotal = side === 'left' ? rightTotal.value : leftTotal.value
  if (!fromTotal || !toTotal) return
  const other = side === 'left' ? 'right' : 'left'
  suppressUntil[other] = Date.now() + 300
  const pane = other === 'left' ? leftPane.value : rightPane.value
  // 页码按总页数比例换算 + 页内偏移保持
  const target = (pos.page - 1 + pos.ratio) / fromTotal * toTotal + 1
  pane?.scrollToPos(target)
}

function fromLeftScroll(pos: { page: number; ratio: number }) { syncFrom('left', pos) }
function fromRightScroll(pos: { page: number; ratio: number }) { syncFrom('right', pos) }

function fromLeft(n: number) { leftPage.value = n }
function fromRight(n: number) { rightPage.value = n }

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
      <span class="muted" style="color: var(--r-muted, #cfd4dc)">
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
        @scroll-pos="fromLeftScroll"
        @loaded="(n: number) => (leftTotal = n)"
      />
      <PdfPane
        ref="rightPane"
        class="half"
        :url="rightUrl"
        :zoom="zoom"
        :expected-total="expectedTotal"
        @page-change="fromRight"
        @scroll-pos="fromRightScroll"
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
  background: var(--r-panel, #2b2f33);
  color: var(--r-text, #e5e7eb);
  border-bottom: 1px solid var(--r-border, transparent);
  flex-shrink: 0;
  transition: background .25s;
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
