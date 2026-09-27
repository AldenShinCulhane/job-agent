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
  /** Newest confirmed version: what the pipeline resumes on, and what a
   * discard falls back to. Lower than `version` while an unconfirmed draft
   * sits on top of it — the pipeline is paused meanwhile, not running on
   * it. Null before the first confirm. */
  live_version?: number | null
  discarded?: number[]
  superseded?: number[]
  profile?: Profile
  ledger?: Record<string, unknown>
  extraction_method?: string
}

export type Provider = 'claude' | 'codex'

export const PROVIDERS: readonly Provider[] = ['claude', 'codex']

/** A model the provider lists, as the engine picker offers it. `efforts`
 * is empty for a model that takes no effort setting. */
export interface ModelOption {
  id: string
  label: string
  description: string
  efforts: string[]
}

export interface ModelState {
  status: string | null
  last_error: string | null
  verified_at: string | null
}

/** One provider's state, for one model. `status` combines the provider's
 * own state (sign-in, usage limit, setup) with the model's (entitlement,
 * and whether it can run under the app's lockdown):
 * ok | unprobed | not_entitled | unavailable | auth_required |
 * config_error | waiting_usage_exhausted. */
export interface EngineStatus {
  provider: Provider
  status: string
  model: string | null
  retry_in_seconds: number | null
  /** The app's own estimate of its next automatic retry. */
  next_retry_at: string | null
  /** When the provider said the limit resets; null when it did not say. */
  limit_resets_at: string | null
  /** One usage limit, from the first hit until a call succeeds again. */
  episode: string | null
  last_error: string | null
  /** The last local sign-in check; null before the first. */
  credentials: { present: boolean; problem: string | null; checked_at: string } | null
  verified_at: string | null
}

export interface ProviderView extends EngineStatus {
  catalog: ModelOption[] | null
  catalog_error: string | null
  models: Record<string, ModelState>
  /** Claude only: the model the default selection settled on. */
  auto_model?: string | null
}

export interface Selection {
  provider: Provider
  /** Null only for Claude's default (Fable 5 when the plan has it, else
   * Opus 5). */
  model: string | null
}

/** Both engines, as GET /api/engines answers: the one source App reads
 * engine state from. */
export interface EnginesView {
  selection: Selection
  providers: Record<Provider, ProviderView>
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
  /** Which build of these inputs this is: 1 unless Try again or
   * Regenerate made another. */
  attempt: number
  /** The package a Try again or Regenerate was pressed on, if one was. */
  retry_of: number | null
  /** What the user would call each input that differs from `retry_of`'s
   * ("profile", "posting", "page limit", "templates"); empty for another
   * attempt of the same inputs. */
  inputs_changed: string[]
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

export interface SelectionResult extends ProbeResult {
  selection: Selection
}

export const api = {
  engines: () => fetch('/api/engines').then(toJson<EnginesView>),
  /** Re-read a provider's model list. No prompt is spent. */
  refreshCatalog: (provider: Provider) =>
    fetch(`/api/engines/${provider}/catalog`, { method: 'POST' }).then(
      toJson<EnginesView>,
    ),
  /** The one model for every task. A model not yet proven on this plan
   * is checked before this answers. */
  selectEngine: (provider: Provider, model: string | null) =>
    fetch('/api/engine/selection', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, model }),
    }).then(toJson<SelectionResult>),
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
  discardDraft: () =>
    fetch('/api/profile/discard', { method: 'POST' }).then(toJson<ProfilePayload>),
  confirmProfile: (version: number) =>
    fetch('/api/profile/confirm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version }),
    }).then(toJson<ProfilePayload>),
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
  /** `runId` is the run the selection was made against — the server
   * rejects ids that are not in it, so a stale selection fails loudly
   * instead of generating a package for a job the user never saw. */
  packagesGenerate: (jobIds: string[], runId: number | null) =>
    fetch('/api/packages/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job_ids: jobIds, run_id: runId }),
    }).then(toJson<{ package_ids: number[]; created: number }>),
  /** Try again (failed) or Regenerate (done). `requestKey` is one per
   * click: sending it again returns the package that click already got.
   * Rejects with `RetryUnconfirmed` when no answer says what happened. */
  packageRetry: (packageId: number, requestKey: string) =>
    fetch(`/api/packages/${packageId}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_key: requestKey }),
    }).then(
      (res) => {
        if (res.status >= 500) throw new RetryUnconfirmed()
        return toJson<{
          package_id: number
          created: boolean
          attempt: number
          new_inputs: boolean
        }>(res)
      },
      () => {
        throw new RetryUnconfirmed()
      },
    ),
}

/** A retry whose answer never arrived, or came back as a server error.
 * Neither says the click failed — it may have made the package — so the
 * page says it could not confirm, and reads the list to find out. */
export class RetryUnconfirmed extends Error {
  constructor() {
    super("Couldn't confirm the retry response; checking package status")
    this.name = 'RetryUnconfirmed'
  }
}

export const packageFileUrl = (packageId: number, name: string, inline = false) =>
  `/api/packages/${packageId}/file/${name}${inline ? '?inline=1' : ''}`
