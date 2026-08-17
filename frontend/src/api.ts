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

export const api = {
  status: () =>
    fetch('/api/status').then(toJson<{ app: string; engine: EngineStatus }>),
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
}
