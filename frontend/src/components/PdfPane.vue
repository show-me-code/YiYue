<script setup lang="ts">
/**
 * 单栏 PDF 懒渲染面板：连续滚动、按需渲染、可缩放。
 * 右侧"译文"面板在翻译未完成时，用占位块表示尚未翻译的页。
 */
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

const props = withDefaults(defineProps<{
  url: string | null
  /** 期望总页数（> 文档实际页数时，超出部分显示"翻译中"占位） */
  expectedTotal?: number
  /** 缩放倍率，1 = 适配宽度 */
  zoom?: number
}>(), { expectedTotal: 0, zoom: 1 })

const emit = defineEmits<{
  (e: 'page-change', n: number): void
  /** 连续滚动位置（当前页 + 页内偏移比例），用于双栏页锚点同步 */
  (e: 'scroll-pos', pos: { page: number; ratio: number }): void
  (e: 'loaded', total: number): void
  (e: 'error', message: string): void
}>()

const container = ref<HTMLElement | null>(null)
const docPages = ref(0)
const docTotal = ref(0)
const loading = ref(false)
const errorText = ref('')

const displayTotal = computed(() =>
  Math.max(docTotal.value, props.expectedTotal ?? 0))

let doc: any = null
let renderTasks = new Map<number, any>()
const renderedPages = new Set<number>()
let pageTops: number[] = []
let fitScale = 1
/** 每页在 scale=1 下的尺寸：加载时一次性取出，预置 slot 真实高度 */
let pageDims: { w: number; h: number }[] = []
let observer: IntersectionObserver | null = null
let resizeObs: ResizeObserver | null = null
let resizeTimer: ReturnType<typeof setTimeout> | null = null
let loadingTask: any = null
/** 载入代次：增量合并会频繁 reload，旧请求晚到的结果必须丢弃 */
let loadSeq = 0
/** 已渲染画布的保留半径（页）：远处的画布要回收，否则内存随页数无上限增长 */
const KEEP_RADIUS = 8

const slotRefs = new Map<number, HTMLElement>()

function setSlotRef(i: number, el: any) {
  if (el) slotRefs.set(i, el as HTMLElement)
  else slotRefs.delete(i)
}

async function load() {
  const seq = ++loadSeq
  destroyDoc()
  if (!props.url) return
  loading.value = true
  errorText.value = ''
  try {
    const task = pdfjsLib.getDocument({
      url: props.url,
      // 译文 PDF 用 Tectonic + Fandol 生成，汉字是 CID-keyed 字体：
      // 不带 cMaps 时 pdf.js 解不出字形，整段中文直接消失
      cMapUrl: '/pdfjs/cmaps/',
      cMapPacked: true,
      standardFontDataUrl: '/pdfjs/standard_fonts/',
    })
    loadingTask = task
    const loaded = await task.promise
    if (seq !== loadSeq) { void task.destroy(); return }
    doc = loaded
    docTotal.value = doc.numPages
    // 用第一页真实宽度计算适配比例，避免首屏渲染比例失真
    const first = await doc.getPage(1)
    if (seq !== loadSeq) return
    const w = first.getViewport({ scale: 1 }).width
    const el0 = container.value
    if (el0) fitScale = Math.max(0.2, (el0.clientWidth - 24) / w)
    // 预置每页真实高度：懒渲染只延迟画布绘制，布局高度不再随渲染
    // 增长而漂移——否则同步滚动按滚动比例定位时，目标栏的总高度还在
    // 变化，落点会错（旧的 200px 占位高度会让对侧"弹回"前面的页）
    pageDims = []
    pageDims[1] = { w, h: first.getViewport({ scale: 1 }).height }
    for (let i = 2; i <= doc.numPages; i++) {
      const pg = await doc.getPage(i)
      if (seq !== loadSeq) return
      const vp = pg.getViewport({ scale: 1 })
      pageDims[i] = { w: vp.width, h: vp.height }
    }
    await nextFrame()  // 等 slot 节点渲染出来，slotRefs 才有值
    if (seq !== loadSeq) return
    applyHeights()
    observeSlots()
    renderVisible()
    watchResize()
    // loaded 放在 slot 就绪之后：订阅方收到后立刻 goToPage 才有节点可跳
    emit('loaded', docTotal.value)
  } catch (e) {
    // 被新的 load() 或 destroyDoc() 中止的旧请求不报错，否则界面上会留着
    // 一条 "Loading aborted" 把已经加载好的文档挡住
    if (seq !== loadSeq) return
    errorText.value = (e as Error).message ?? String(e)
    emit('error', errorText.value)
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

function nextFrame(): Promise<void> {
  return new Promise(r => requestAnimationFrame(() => r()))
}

function destroyDoc() {
  observer?.disconnect()
  observer = null
  resizeObs?.disconnect()
  resizeObs = null
  for (const t of renderTasks.values()) t?.cancel?.()
  renderTasks = new Map()
  renderedPages.clear()
  slotRefs.clear()
  docTotal.value = 0
  docPages.value = 0
  pageTops = []
  pageDims = []
  if (loadingTask) { loadingTask.destroy?.(); loadingTask = null }
  doc = null
}

function pageScale(): number {
  return fitScale * props.zoom
}

/** 按缓存的页面尺寸预置所有 slot 高度（缩放变化后也要重设）。 */
function applyHeights() {
  for (const [i, el] of slotRefs.entries()) {
    const dim = pageDims[i]
    if (dim) el.style.height = `${Math.floor(dim.h * pageScale())}px`
  }
}

function observeSlots() {
  observer = new IntersectionObserver((entries) => {
    for (const en of entries) {
      const idx = Number((en.target as HTMLElement).dataset.page)
      if (en.isIntersecting) {
        if (idx <= docTotal.value) renderPage(idx)
        else renderPlaceholder(idx)
      }
    }
  }, { root: container.value, rootMargin: '400px 0px' })
  for (const el of slotRefs.values()) observer.observe(el)
}

async function renderPage(i: number) {
  if (!doc || renderedPages.has(i) || renderTasks.has(i)) return
  const el = slotRefs.get(i)
  if (!el) return
  renderedPages.add(i)
  try {
    const page = await doc.getPage(i)
    const scale = pageScale()
    const viewport = page.getViewport({ scale })
    const dpr = window.devicePixelRatio || 1
    let canvas = el.querySelector('canvas') as HTMLCanvasElement | null
    if (!canvas) {
      canvas = document.createElement('canvas')
      el.appendChild(canvas)
    }
    canvas.width = Math.floor(viewport.width * dpr)
    canvas.height = Math.floor(viewport.height * dpr)
    canvas.style.width = `${Math.floor(viewport.width)}px`
    canvas.style.height = `${Math.floor(viewport.height)}px`
    el.style.height = `${Math.floor(viewport.height)}px`
    const ctx = canvas.getContext('2d')!
    const task = page.render({ canvasContext: ctx, viewport, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : undefined })
    renderTasks.set(i, task)
    await task.promise
    recordTop(i, Math.floor(viewport.height))
  } catch { renderedPages.delete(i) } finally {
    renderTasks.delete(i)
  }
}

function renderPlaceholder(i: number) {
  const el = slotRefs.get(i)
  if (!el || el.querySelector('.ph-inner')) return
  const div = document.createElement('div')
  div.className = 'ph-inner'
  div.textContent = `第 ${i} 页 · 翻译中…`
  el.appendChild(div)
  el.style.height = '900px'
}

function recordTop(i: number, height: number) {
  while (pageTops.length < i) pageTops.push(0)
  pageTops[i - 1] = (pageTops[i - 2] ?? 0) + height
}

function onScroll() {
  const el = container.value
  if (!el) return
  // 注意：slotRefs 是 Map，插入序不保证是页码序（v-for 键变化重挂载时
  // Vue 的 ref 回调可能以倒序注册）。当前页计算依赖顺序，必须显式排序。
  const ordered = [...slotRefs.entries()].sort((a, b) => a[0] - b[0])
  let current = 1
  for (const [i, node] of ordered) {
    if (node.offsetTop - el.scrollTop <= el.clientHeight * 0.35) current = Math.max(current, i)
    else break
  }
  docPages.value = Math.min(current, docTotal.value)
  evictFar(current)
  emit('page-change', current)
  emit('scroll-pos', currentPos(el))
}

/** 当前阅读位置：以"35% 视口线"为准，返回页码 + 页内偏移比例（0~1）。 */
function currentPos(el: HTMLElement): { page: number; ratio: number } {
  const page = Math.max(1, docPages.value)
  const node = slotRefs.get(page)
  if (!node) return { page, ratio: 0 }
  const line = el.scrollTop + el.clientHeight * 0.35
  const h = node.offsetHeight || 1
  const ratio = Math.min(1, Math.max(0, (line - node.offsetTop) / h))
  return { page, ratio }
}

/** 滚动到 pageFloat 指定的位置（3.4 = 第 3 页的 40% 处）。页锚点同步用。 */
function scrollToPos(pageFloat: number) {
  const el = container.value
  if (!el || !docTotal.value) return
  const p = Math.min(docTotal.value, Math.max(1, Math.floor(pageFloat)))
  const node = slotRefs.get(p)
  if (!node) return
  const frac = Math.min(0.999, Math.max(0, pageFloat - p))
  el.scrollTop = Math.max(0,
    node.offsetTop + frac * (node.offsetHeight || 0) - el.clientHeight * 0.35)
}

/** 回收远离当前页的画布：连续滚动长文档时画布个数必须有上限。 */
function evictFar(center: number) {
  if (renderedPages.size <= KEEP_RADIUS * 2 + 1) return
  for (const i of Array.from(renderedPages)) {
    if (Math.abs(i - center) <= KEEP_RADIUS) continue
    renderTasks.get(i)?.cancel?.()
    renderTasks.delete(i)
    renderedPages.delete(i)
    const el = slotRefs.get(i)
    if (el) {
      // 只丢画布位图，**保留占位高度**：高度一清零，被回收页后面的内容
      // 会整体上移，滚动位置就会跳。
      const height = el.style.height
      el.innerHTML = ''
      el.style.height = height
    }
  }
}

function goToPage(n: number) {
  const el = container.value
  const node = slotRefs.get(Math.max(1, n))
  if (el && node) {
    el.scrollTop = node.offsetTop - 12
    docPages.value = Math.min(n, docTotal.value)
  }
}

function clearRendered() {
  for (const t of renderTasks.values()) t?.cancel?.()
  renderTasks = new Map()
  renderedPages.clear()
  pageTops = []
  for (const el of slotRefs.values()) {
    el.innerHTML = ''
    el.style.height = ''
  }
  applyHeights()  // 高度用当前缩放重新预置，而不是回到 200px 占位
  renderVisible()
}

function renderVisible() {
  const el = container.value
  if (!el) return
  const scrollTop = el.scrollTop
  const viewBottom = scrollTop + el.clientHeight + 600
  for (const [i, node] of slotRefs.entries()) {
    if (node.offsetTop < viewBottom && node.offsetTop + 600 > scrollTop) {
      if (i <= docTotal.value) renderPage(i)
      else renderPlaceholder(i)
    }
  }
}

/** 容器宽度变化（切换单/双栏、开关 AI 面板、窗口缩放）时重算适配比例。 */
function watchResize() {
  const el = container.value
  if (!el || resizeObs) return
  resizeObs = new ResizeObserver(() => {
    if (resizeTimer) clearTimeout(resizeTimer)
    resizeTimer = setTimeout(() => {
      if (!doc || !pageDims[1]) return
      const newFit = Math.max(0.2, (el.clientWidth - 24) / pageDims[1].w)
      if (Math.abs(newFit - fitScale) < 0.01) return
      fitScale = newFit
      clearRendered()
    }, 150)
  })
  resizeObs.observe(el)
}

watch(() => props.url, load)
watch(() => props.zoom, () => {
  clearRendered()
})

defineExpose({ goToPage, reload: load, clearRendered, scrollToPos,
  currentPage: () => docPages.value, docTotal })

onMounted(load)
onBeforeUnmount(destroyDoc)
</script>

<template>
  <div class="pane">
    <div v-if="!url && !errorText" class="pane-empty">等待输出…</div>
    <div v-else-if="errorText" class="pane-empty error">{{ errorText }}</div>
    <div v-else ref="container" class="pane-scroll" @scroll.passive="onScroll">
      <div v-for="i in displayTotal" :key="i" :data-page="i"
        :ref="el => setSlotRef(i, el)" class="page-slot">
      </div>
    </div>
    <div v-if="loading" class="pane-loading">加载中…</div>
  </div>
</template>

<style scoped>
.pane {
  position: relative;
  height: 100%;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: #525659;
}
.pane-scroll {
  flex: 1;
  overflow: auto;
  padding: 12px 12px 48px;
}
.page-slot {
  position: relative;
  margin: 0 auto 12px;
  background: #fff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.35);
  min-height: 200px;
  overflow: hidden;
}
.page-slot canvas {
  display: block;
  margin: 0 auto;
}
.ph-inner {
  position: sticky;
  top: 45%;
  text-align: center;
  color: #909399;
  font-size: 15px;
  padding-top: 380px;
}
.pane-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #cfd4dc;
  font-size: 14px;
}
.pane-empty.error {
  color: #f89898;
  padding: 20px;
  text-align: center;
}
.pane-loading {
  position: absolute;
  top: 8px;
  right: 12px;
  color: #cfd4dc;
  font-size: 12px;
}
</style>
