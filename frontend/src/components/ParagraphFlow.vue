<script setup lang="ts">
/** 逐段对照阅读：沉浸式深色阅读流——英文段落在前、中文译文紧挨其下，
 *  段落之间无卡片无隔断，形成连续文档；新段落带渐入动画。
 *  arXiv 任务走源码级抽取：行内/展示公式经 KaTeX 排版，引用渲染为角标。 */
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import { api, type ParagraphPair } from '../api/client'

const props = defineProps<{
  taskId: number
  /** 译文增量更新信号（页数增加时变化），触发段落重取 */
  reloadKey: number
  /** 任务是否仍在进行中（进行中才跟随 reloadKey 刷新） */
  live: boolean
}>()

const pairs = ref<ParagraphPair[]>([])
const ready = ref(false)
const loading = ref(true)
const showSource = ref(true)
const sourceCount = ref(0)
const targetCount = ref(0)
const macros = ref<Record<string, string>>({})
let debounce: ReturnType<typeof setTimeout> | null = null

/** 阅读区底色预设（护眼向）。通过覆盖 --r-* 变量生效，跟随主题的深浅色切换不受影响。 */
const BG_PRESETS: { key: string; label: string; swatch: string; vars: Record<string, string> }[] = [
  { key: 'theme', label: '跟随主题', swatch: '', vars: {} },
  {
    key: 'paper', label: '纸白', swatch: '#fafaf7',
    vars: {
      '--r-bg': '#fafaf7', '--r-text': '#26282c', '--r-src': '#71767e',
      '--r-src-hover': '#4b5058', '--r-heading': '#16181c', '--r-muted': '#9aa0a8',
      '--r-soft': '#666c75', '--r-chip': '#d8d5cd', '--r-border': '#e6e3da',
      '--r-dots': '#d5d2c8', '--r-code-bg': '#f2f1ec',
    },
  },
  {
    key: 'parchment', label: '羊皮纸', swatch: '#f3e9cf',
    vars: {
      '--r-bg': '#f3e9cf', '--r-text': '#4a3d26', '--r-src': '#8d7c56',
      '--r-src-hover': '#6b5c3d', '--r-heading': '#3a3018', '--r-muted': '#a89770',
      '--r-soft': '#7d6d4a', '--r-chip': '#dbcaa4', '--r-border': '#e3d5b4',
      '--r-dots': '#d8c89f', '--r-code-bg': '#ece1c5',
    },
  },
  {
    key: 'greentea', label: '护眼绿', swatch: '#d9ecd9',
    vars: {
      '--r-bg': '#d9ecd9', '--r-text': '#233b28', '--r-src': '#5d7a63',
      '--r-src-hover': '#3f5c45', '--r-heading': '#16291b', '--r-muted': '#7d9a83',
      '--r-soft': '#4e6b54', '--r-chip': '#b5d4b9', '--r-border': '#c4dfc8',
      '--r-dots': '#b9d4bd', '--r-code-bg': '#cfe4d0',
    },
  },
]
const bgKey = ref(localStorage.getItem('yiyue-flow-bg') || 'theme')
const bgVars = computed(
  () => BG_PRESETS.find(p => p.key === bgKey.value)?.vars ?? {})

function setBg(key: string) {
  bgKey.value = key
  localStorage.setItem('yiyue-flow-bg', key)
}

async function load() {
  try {
    const data = await api.getParagraphs(props.taskId)
    pairs.value = data.pairs
    ready.value = data.ready
    sourceCount.value = data.source_count
    targetCount.value = data.target_count
    macros.value = data.macros ?? {}
  } catch (e) {
    ElMessage.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

onMounted(load)

watch(() => props.reloadKey, () => {
  // 增量合并会频繁触发：防抖 1.5s，避免每页都重抽一次段落
  if (!props.live) return
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(load, 1500)
})

/** 首屏段落渐入：依次错开 30ms，超过 20 段后不再延迟（避免长文后段干等） */
function enterDelay(i: number) {
  return `${Math.min(i, 20) * 30}ms`
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

/** 渲染一段富文本：$...$/$$...$$ 走 KaTeX，⟦1,2⟧ → 引用角标，⟦§:label⟧ → § */
function renderRich(s: string): string {
  if (!s) return ''
  return s.split(/(\$\$[\s\S]+?\$\$|\$(?:\\.|[^$\\])+\$)/g).map(part => {
    if (part.startsWith('$$') && part.endsWith('$$')) {
      return katex.renderToString(part.slice(2, -2),
        { displayMode: true, macros: macros.value, throwOnError: false })
    }
    if (part.length > 2 && part.startsWith('$') && part.endsWith('$')) {
      return katex.renderToString(part.slice(1, -1),
        { displayMode: false, macros: macros.value, throwOnError: false })
    }
    return escapeHtml(part)
      .replace(/⟦([\d,]+)⟧/g, '<sup class="cite">[$1]</sup>')
      .replace(/⟦§:([^⟧]+)⟧/g, '<sup class="refx" title="$1">§</sup>')
  }).join('')
}
</script>

<template>
  <div class="para-flow" :style="bgVars">
    <div class="flow-toolbar">
      <span class="flow-title">逐段对照</span>
      <span class="bg-swatches">
        <span
          v-for="p in BG_PRESETS"
          :key="p.key"
          class="swatch"
          :class="{ active: bgKey === p.key, theme: p.key === 'theme' }"
          :style="p.swatch ? { background: p.swatch } : {}"
          :title="p.label"
          @click="setBg(p.key)"
        >{{ p.key === 'theme' ? 'A' : '' }}</span>
      </span>
      <span style="flex:1" />
      <span v-if="ready && sourceCount !== targetCount" class="muted">
        原文 {{ sourceCount }} 段 / 译文 {{ targetCount }} 段
      </span>
      <span
        class="src-toggle"
        :class="{ off: !showSource }"
        @click="showSource = !showSource"
      >
        <span class="dot" />原文
      </span>
    </div>

    <div v-if="loading" v-loading="true" class="flow-center" />
    <div v-else-if="!ready" class="flow-center">
      <el-empty description="译文尚未生成，逐段对照将在翻译输出后可用" />
    </div>

    <div v-else class="flow-body">
      <article class="flow-doc">
        <div
          v-for="p in pairs"
          :key="p.i"
          class="pair"
          :class="[p.kind ?? 'para', 'lv' + (p.level ?? 0)]"
          :style="{ animationDelay: enterDelay(p.i) }"
        >
          <!-- 展示公式：不翻译，渲染一次居中展示 -->
          <div
            v-if="p.kind === 'math'"
            class="math-block"
            v-html="renderRich(p.target || p.source)"
          />
          <!-- 插图：图片只渲染一次（中文区不重复），图注先英文后中文 -->
          <div v-else-if="p.kind === 'figure'" class="figure-block">
            <div class="fig-images">
              <img
                v-for="img in p.images ?? []"
                :key="img"
                :src="`/api/tasks/${taskId}/figimage?path=${encodeURIComponent(img)}`"
                loading="lazy"
                alt=""
              >
            </div>
            <div
              v-if="showSource && p.source"
              class="src"
              v-html="renderRich(p.source)"
            />
            <div v-if="p.target" class="tgt" v-html="renderRich(p.target)" />
          </div>
          <!-- 算法/代码块：等宽字体，公式仍渲染 -->
          <div v-else-if="p.kind === 'code'" class="code-wrap">
            <pre
              v-if="showSource"
              class="code-block src-code"
              v-html="renderRich(p.source)"
            />
            <pre class="code-block" v-html="renderRich(p.target)" />
          </div>
          <template v-else>
            <div
              v-if="showSource"
              class="src"
              v-html="renderRich(p.source)"
            />
            <div class="tgt" v-html="renderRich(p.target)" />
          </template>
        </div>
        <div v-if="live" class="flow-more">
          <span class="pulse" />翻译进行中，后续段落将陆续追加…
        </div>
      </article>
    </div>
  </div>
</template>

<style scoped>
.para-flow {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  background: var(--r-bg, #141518);
  transition: background .25s;
}

/* ---------- 工具栏 ---------- */
.flow-toolbar {
  height: 44px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 20px;
  background: var(--r-bg, rgba(20, 21, 24, 0.9));
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--r-border, #26282e);
  flex-shrink: 0;
}
.flow-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--r-soft, #8b8f98);
  letter-spacing: 2px;
}
.bg-swatches {
  display: inline-flex;
  gap: 6px;
  margin-left: 12px;
}
.swatch {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  border: 1px solid var(--r-chip, #33363d);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  color: var(--r-soft, #8b8f98);
  transition: transform .2s, box-shadow .2s;
}
.swatch.theme {
  background: linear-gradient(135deg, #141518 50%, #f5f6f8 50%);
}
.swatch:hover {
  transform: scale(1.15);
}
.swatch.active {
  box-shadow: 0 0 0 2px var(--r-accent, #4a90d9);
}
.muted {
  font-size: 12px;
  color: var(--r-muted, #5c616b);
}
.src-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--r-soft, #a8b4c4);
  cursor: pointer;
  user-select: none;
  padding: 4px 10px;
  border: 1px solid var(--r-chip, #33363d);
  border-radius: 999px;
  transition: color .25s, border-color .25s, background .25s;
}
.src-toggle:hover {
  border-color: var(--r-accent, #4a90d9);
  color: var(--r-accent-text, #cfe3f7);
}
.src-toggle .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--r-accent, #4a90d9);
  transition: background .25s;
}
.src-toggle.off {
  color: var(--r-muted, #5c616b);
}
.src-toggle.off .dot {
  background: var(--r-chip, #3a3d44);
}

/* ---------- 正文阅读流 ---------- */
.flow-center {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}
.flow-center :deep(.el-empty__description p) {
  color: var(--r-muted, #5c616b);
}
.flow-center :deep(.el-loading-text) {
  color: var(--r-soft, #8b8f98);
}
.flow-body {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  scrollbar-width: thin;
  scrollbar-color: var(--r-chip, #33363d) transparent;
}
.flow-body::-webkit-scrollbar {
  width: 8px;
}
.flow-body::-webkit-scrollbar-thumb {
  background: var(--r-chip, #33363d);
  border-radius: 4px;
}
.flow-doc {
  max-width: 780px;
  margin: 0 auto;
  padding: 48px 40px 120px;
}

/* 段落对：无卡片、无间隔线，先英文后译文，形成连续文档 */
.pair {
  margin: 0 0 1.6em;
  animation: pair-in .5s cubic-bezier(.22, .8, .32, 1) both;
}
@keyframes pair-in {
  from {
    opacity: 0;
    transform: translateY(14px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.src {
  margin: 0;
  font-family: Georgia, 'Times New Roman', 'Songti SC', serif;
  font-size: 16.5px;
  line-height: 1.75;
  color: var(--r-src, #9ba1ab);
  transition: color .25s;
}
.pair:hover .src {
  color: var(--r-src-hover, #b7bdc7);
}
.tgt {
  margin: 0.45em 0 0;
  font-size: 16px;
  line-height: 1.95;
  letter-spacing: 0.02em;
  color: var(--r-text, #e9ebf0);
  /* 译文下压一条极浅的点线，区分于原文又不切断阅读流 */
  background-image: linear-gradient(to right, var(--r-dots, #3d4149) 1px, transparent 1px);
  background-size: 6px 1px;
  background-repeat: repeat-x;
  background-position: 0 100%;
  padding-bottom: 3px;
  transition: color .25s;
}
.pair:hover .tgt {
  color: var(--r-heading, #ffffff);
}

/* 标题段 */
.pair.heading .src,
.pair.heading .tgt {
  font-weight: 700;
  background: none;
  padding-bottom: 0;
}
.pair.heading.lv1 .src { font-size: 24px; line-height: 1.4; color: var(--r-src-hover, #c9cdd4); }
.pair.heading.lv1 .tgt { font-size: 22px; line-height: 1.5; margin-top: .3em; }
.pair.heading.lv2 { margin-top: 2.2em; }
.pair.heading.lv2 .src { font-size: 19px; color: var(--r-src-hover, #c9cdd4); }
.pair.heading.lv2 .tgt { font-size: 18px; margin-top: .25em; }
.pair.heading.lv3 .src,
.pair.heading.lv3 .tgt { font-size: 16.5px; }
.pair.heading .tgt { color: var(--r-heading, #f2f3f5); }

/* 展示公式 */
.pair.math {
  margin: 0 0 1.8em;
  overflow-x: auto;
}
.math-block {
  text-align: center;
  padding: 10px 0;
  color: var(--r-text, #d7dae0);
  font-size: 17px;
}

/* 插图 */
.pair.figure {
  margin: 0 0 2em;
}
.fig-images {
  display: flex;
  gap: 12px;
  justify-content: center;
  align-items: flex-start;
  margin-bottom: 12px;
}
.fig-images img {
  max-width: 100%;
  flex: 1 1 0;
  min-width: 0;
  object-fit: contain;
  background: #fff;
  border-radius: 8px;
  border: 1px solid var(--r-border, #2c2f36);
  padding: 6px;
  transition: transform .25s, box-shadow .25s;
}
.fig-images img:hover {
  transform: scale(1.01);
  box-shadow: 0 4px 24px rgba(0, 0, 0, .45);
}
.pair.figure .src,
.pair.figure .tgt {
  font-size: 13.5px;
  line-height: 1.7;
}
.pair.figure .src { color: var(--r-muted, #7d828c); }
.pair.figure .tgt { color: var(--r-soft, #b9bec8); }

/* 算法/代码块 */
.pair.code {
  margin: 0 0 1.8em;
}
.code-block {
  margin: 0;
  padding: 14px 18px;
  background: var(--r-code-bg, #1a1c20);
  border: 1px solid var(--r-border, #2c2f36);
  border-radius: 8px;
  font-family: 'Cascadia Code', Consolas, 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.8;
  color: var(--r-text, #c9d1d9);
  white-space: pre-wrap;
  word-break: break-word;
  overflow-x: auto;
}
.src-code {
  margin-bottom: 8px;
  color: var(--r-soft, #8b8f98);
}

/* 图注/表注 */
.pair.caption .src,
.pair.caption .tgt {
  font-size: 13.5px;
  line-height: 1.7;
}
.pair.caption .src { color: var(--r-muted, #7d828c); }
.pair.caption .tgt { color: var(--r-soft, #b9bec8); }

/* 列表项 */
.pair.item { margin-bottom: 1em; }

/* 引用与交叉引用角标 */
.src :deep(.cite),
.tgt :deep(.cite) {
  color: var(--r-accent, #7fb4e8);
  font-size: .72em;
  margin-left: 1px;
}
.src :deep(.refx),
.tgt :deep(.refx) {
  color: #a06ec4;
  font-size: .72em;
  cursor: help;
}

/* KaTeX 颜色跟随正文 */
.src :deep(.katex) { color: inherit; font-size: 1.02em; }
.tgt :deep(.katex) { color: inherit; font-size: 1.04em; }
.math-block :deep(.katex-display) { margin: 0; }

/* ---------- 进行中提示 ---------- */
.flow-more {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 32px;
  font-size: 13px;
  color: var(--r-muted, #5c616b);
}
.pulse {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--r-accent, #4a90d9);
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse {
  0%, 100% {
    opacity: .3;
    transform: scale(.8);
  }
  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}
</style>
