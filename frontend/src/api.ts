export interface Contact {
  phone: string | null
  email: string | null
  linkedin: string | null
  github: string | null
  website: string | null
  location: string | null
}

export interface Experience {
  title: string
  company: string
  location: string | null
  start: string
  end: string
  bullets: string[]
}

export interface Project {
  name: string
  technologies: string[]
  date: string | null
  link: string | null
  bullets: string[]
}

export interface Education {
  institution: string
  location: string | null
  degree: string
  start: string | null
  end: string | null
  details: string[]
}

export interface SkillGroup {
  category: string
  items: string[]
}

export interface Profile {
  name: string
  headline: string | null
  contact: Contact
  experiences: Experience[]
  projects: Project[]
  education: Education[]
  skills: SkillGroup[]
}

export interface ProfilePayload {
  version: number | null
  confirmed?: boolean
  profile?: Profile
  ledger?: Record<string, unknown>
  extraction_method?: string
}

export interface EngineStatus {
  status: string
  model: string | null
  retry_in_seconds: number | null
  last_error: string | null
}

export interface LinkedInStatus {
  state: string
  detail: string | null
}

export interface ScoreBreakdown {
  skills: number
  experience: number
  education: number
  matched_skills: string[]
  missing_skills: string[]
  match_reasons: string[]
  gap_reasons: string[]
  exceeds_experience: boolean
  user_years: number | null
}

export interface Job {
  job_id: string
  title: string
  company: string
  location: string | null
  posted_text: string | null
  workplace: string | null
  easy_apply: boolean
  url: string
  first_seen_at: string
  last_seen_at: string
  description: string | null
  description_hash: string | null
  detail_fetched_at: string | null
  deterministic_score: number | null
  llm_rerank: number | null
  llm_rank: number | null
  score_breakdown: ScoreBreakdown | null
}

export interface Explanation {
  strengths: string[]
  gaps: string[]
  pitch: string
}

export interface JobDetail extends Job {
  refetched: boolean
  changed: boolean
}

export interface Package {
  id: number
  job_id: string
  profile_version: number
  jd_hash: string
  page_limit: number
  status: string
  error: string | null
  retry_count: number
  output_dir: string | null
  created_at: string
  updated_at: string
  title: string
  company: string
  url: string
  provenance: Record<string, unknown>
}

export interface SearchConfig {
  keywords: string
  location?: string | null
  date_posted?: string | null
  workplace?: string[]
  experience?: string[]
  cap?: number | null
}

export interface SearchRun {
  id: number | null
  config?: SearchConfig
  status?: string
  next_start?: number
  collected?: number
  message?: string | null
  started_at?: string
  updated_at?: string
}

async function toJson<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = ((await res.json()) as { detail?: string }).detail ?? detail
    } catch {
      /* not json */
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export interface Settings {
  resume_page_limit: number
  engine: EngineStatus
  latex_engines: string[]
}

export interface ProbeResult {
  ok: boolean
  verified: boolean
  model?: string | null
  error?: string
  detail?: string
  engine?: EngineStatus
}

export const api = {
  status: () =>
    fetch('/api/status').then(toJson<{ app: string; engine: EngineStatus }>),
  getSettings: () => fetch('/api/settings').then(toJson<Settings>),
  putSettings: (resumePageLimit: number) =>
    fetch('/api/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ resume_page_limit: resumePageLimit }),
    }).then(toJson<{ resume_page_limit: number }>),
  reprobeEngine: () =>
    fetch('/api/engine/reprobe', { method: 'POST' }).then(toJson<ProbeResult>),
  getProfile: () => fetch('/api/profile').then(toJson<ProfilePayload>),
  uploadResume: (file: File) => {
    const body = new FormData()
    body.append('file', file)
    return fetch('/api/profile/upload', { method: 'POST', body }).then(
      toJson<ProfilePayload>,
    )
  },
  saveProfile: (profile: Profile) =>
    fetch('/api/profile', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(profile),
    }).then(toJson<ProfilePayload>),
  confirmProfile: (version: number) =>
    fetch('/api/profile/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version }),
    }).then(toJson<{ ok: boolean; version: number }>),
  linkedinStatus: () => fetch('/api/linkedin/status').then(toJson<LinkedInStatus>),
  linkedinConnect: () =>
    fetch('/api/linkedin/connect', { method: 'POST' }).then(toJson<LinkedInStatus>),
  searchStart: (config: SearchConfig) =>
    fetch('/api/search/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }).then(toJson<{ run_id: number; status: string }>),
  searchResume: () =>
    fetch('/api/search/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    }).then(toJson<SearchRun>),
  searchLatest: () => fetch('/api/search/latest').then(toJson<SearchRun>),
  jobs: () =>
    fetch('/api/jobs').then(
      toJson<{
        jobs: Job[]
        run_id: number | null
        scoring_pending: number
        scoring_stall: { reason: string; detail: string | null } | null
      }>,
    ),
  jobDetail: (jobId: string) =>
    fetch(`/api/jobs/${jobId}/detail`, { method: 'POST' }).then(toJson<JobDetail>),
  jobExplain: (jobId: string) =>
    fetch(`/api/jobs/${jobId}/explain`, { method: 'POST' }).then(toJson<Explanation>),
  searchRerank: () =>
    fetch('/api/search/rerank', { method: 'POST' }).then(
      toJson<{ reranked: number }>,
    ),
  retryScoring: () =>
    fetch('/api/search/retry-scoring', { method: 'POST' }).then(
      toJson<{ resumed: boolean }>,
    ),
  packages: () =>
    fetch('/api/packages').then(
      toJson<{ packages: Package[]; page_limit: number }>,
    ),
  packagesGenerate: (jobIds: string[]) =>
    fetch('/api/packages/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: jobIds }),
    }).then(toJson<{ package_ids: number[]; created: number }>),
}

export const packageFileUrl = (packageId: number, name: string) =>
  `/api/packages/${packageId}/file/${name}`
