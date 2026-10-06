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
  arxiv_id: string | null
  cache_hit: boolean
  progress: TaskProgress
  created_at: string
  finished_at: string | null
  outputs: TaskOutput[]
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
}

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
}

/** 订阅任务 SSE 事件流，返回 EventSource 便于关闭。 */
export function openTaskEvents(
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
