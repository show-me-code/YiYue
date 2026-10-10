/** 后端 API 客户端。 */

export interface Profile {
  id: number
  name: string
  base_url: string
  model: string
  protocol: 'openai' | 'anthropic'
  qps: number
  pool_max_workers: number | null
  custom_system_prompt: string | null
  is_default: boolean
  api_key_masked: string
}

export interface ProfileInput {
  name: string
  base_url: string
  api_key: string
  model: string
  protocol: 'openai' | 'anthropic'
  qps: number
  pool_max_workers: number | null
  custom_system_prompt: string | null
  is_default: boolean
}

export interface ProfileTestResult {
  ok: boolean
  detail: string
  protocol: string
  suggest_protocol?: 'openai' | 'anthropic'
}

export interface GlossaryEntry {
  id: number
  source: string
  target: string
  tgt_lang: string
  enabled: boolean
}

export interface TaskOutput {
  kind: 'original' | 'mono' | 'dual'
  url: string
}

export interface TaskProgress {
  part_index?: number
  part_total?: number
  stage?: string
  stage_progress?: number
  overall_progress?: number
  pages_done?: number
}

export interface TaskVerification {
  verdict: string
  reasons: string[]
  notes: string[]
  pages: { translated: number; baseline: number }
  overflow: { translated_pages: number[]; baseline_pages: number[]; new_pages: number[] }
  content: Record<string, unknown>
  url: string
}

export interface Task {
  id: number
  filename: string
  status: 'queued' | 'translating' | 'merging' | 'done' | 'failed' | 'cancelled'
  error: string | null
  page_count: number
  size_bytes: number
  lang_in: string
  lang_out: string
  pages: string | null
  part_size: number
  scan_mode: string
  engine: 'auto' | 'arxiv' | 'babeldoc'
  engine_used: 'arxiv' | 'babeldoc' | null
  line_spread: number
  arxiv_id: string | null
  cache_hit: boolean
  progress: TaskProgress
  verification: TaskVerification | null
  /** ISO-8601（带时区偏移），前端按本地时区格式化 */
  created_at: string | null
  finished_at: string | null
  /** 创建到结束的秒数（后端算好，前端不必再解析两个时间） */
  duration_s: number | null
  outputs: TaskOutput[]
  /** 论文标题（离线从源码/文件名提取）与中文标题、LLM 主题词 */
  title_en: string | null
  title_zh: string | null
  topics: string[]
  archived: boolean
  polish: string
}

export interface ParagraphPair {
  i: number
  page?: number
  kind?: 'para' | 'heading' | 'math' | 'caption' | 'item' | 'figure' | 'code'
  level?: number
  source: string
  target: string
  /** 插图相对路径（tex 模式；经 /api/tasks/{id}/figimage 取图） */
  images?: string[]
}

export interface ParagraphsResult {
  ready: boolean
  /** tex = arXiv 源码级抽取（含 LaTeX 公式与引用标记）；pdf = PDF 文本块抽取 */
  mode?: 'tex' | 'pdf'
  /** 作者自定义宏（KaTeX 格式） */
  macros?: Record<string, string>
  pairs: ParagraphPair[]
  source_count: number
  target_count: number
}

export interface ChatMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface UploadOptions {
  profile_id: number
  lang_in: string
  lang_out: string
  pages: string
  part_size: number
  scan_mode: string
  auto_extract_glossary: boolean
  use_glossary: boolean
  engine: 'auto' | 'arxiv' | 'babeldoc'
  arxiv_id: string
  line_spread: number
  polish: string
}

/** 翻译风格预设（与后端 prompt_presets.POLISH_PRESETS 对应） */
export const POLISH_OPTIONS = [
  { value: 'default', label: '默认（学术规范）' },
  { value: 'faithful', label: '忠实直译' },
  { value: 'polished', label: '流畅润色' },
  { value: 'plain', label: '通俗易懂' },
]

async function handle<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = `${resp.status}`
    try {
      const body = await resp.json()
      detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body)
    } catch { /* 保留状态码 */ }
    throw new Error(detail)
  }
  return resp.json() as Promise<T>
}

export const api = {
  // 配置档
  listProfiles: () => fetch('/api/profiles').then(r => handle<Profile[]>(r)),
  createProfile: (p: ProfileInput) =>
    fetch('/api/profiles', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(p),
    }).then(r => handle<Profile>(r)),
  updateProfile: (id: number, p: ProfileInput) =>
    fetch(`/api/profiles/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(p),
    }).then(r => handle<Profile>(r)),
  deleteProfile: (id: number) =>
    fetch(`/api/profiles/${id}`, { method: 'DELETE' }).then(r => handle<{ ok: boolean }>(r)),
  testProfile: (id: number) =>
    fetch(`/api/profiles/${id}/test`, { method: 'POST' })
      .then(r => handle<ProfileTestResult>(r)),

  // 术语表
  listGlossary: () => fetch('/api/glossary').then(r => handle<GlossaryEntry[]>(r)),
  createGlossary: (e: Omit<GlossaryEntry, 'id'>) =>
    fetch('/api/glossary', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(e),
    }).then(r => handle<GlossaryEntry>(r)),
  updateGlossary: (id: number, e: Omit<GlossaryEntry, 'id'>) =>
    fetch(`/api/glossary/${id}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(e),
    }).then(r => handle<GlossaryEntry>(r)),
  deleteGlossary: (id: number) =>
    fetch(`/api/glossary/${id}`, { method: 'DELETE' }).then(r => handle<{ ok: boolean }>(r)),

  // 任务
  createTask: (file: File | null, o: UploadOptions) => {
    const fd = new FormData()
    if (file) fd.append('file', file)
    for (const [k, v] of Object.entries(o)) fd.append(k, String(v))
    return fetch('/api/tasks', { method: 'POST', body: fd }).then(r => handle<Task>(r))
  },
  listTasks: () => fetch('/api/tasks').then(r => handle<Task[]>(r)),
  getTask: (id: number) => fetch(`/api/tasks/${id}`).then(r => handle<Task>(r)),
  cancelTask: (id: number) =>
    fetch(`/api/tasks/${id}/cancel`, { method: 'POST' }).then(r => handle<{ ok: boolean }>(r)),
  retryTask: (id: number) =>
    fetch(`/api/tasks/${id}/retry`, { method: 'POST' }).then(r => handle<Task>(r)),
  deleteTask: (id: number) =>
    fetch(`/api/tasks/${id}`, { method: 'DELETE' }).then(r => handle<{ ok: boolean }>(r)),
  archiveTask: (id: number, archived: boolean) =>
    fetch(`/api/tasks/${id}/archive`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archived }),
    }).then(r => handle<{ ok: boolean; archived: boolean }>(r)),

  // 逐段对照 + AI 阅读助手
  getParagraphs: (id: number) =>
    fetch(`/api/tasks/${id}/paragraphs`).then(r => handle<ParagraphsResult>(r)),
  chat: (id: number, messages: ChatMessage[]) =>
    fetch(`/api/tasks/${id}/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
    }).then(r => handle<{ reply: string }>(r)),
  getChatHistory: (id: number) =>
    fetch(`/api/tasks/${id}/chat/history`)
      .then(r => handle<{ messages: ChatMessage[] }>(r)),
  saveChatHistory: (id: number, messages: ChatMessage[]) =>
    fetch(`/api/tasks/${id}/chat/history`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages }),
    }).then(r => handle<{ ok: boolean }>(r)),
}

/** 订阅任务 SSE 事件流，返回 EventSource 便于关闭。 */export function openTaskEvents(
  taskId: number,
  onEvent: (ev: Record<string, unknown>) => void,
): EventSource {
  const source = new EventSource(`/api/tasks/${taskId}/events`)
  source.onmessage = (m) => {
    try {
      onEvent(JSON.parse(m.data))
    } catch { /* 忽略坏帧 */ }
  }
  return source
}
