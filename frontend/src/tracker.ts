import type { Application, ApplicationStatus, GroupMember } from './api'
import type { RoleRow } from './jobGroups'
import type { Badge } from './styles'

/** What each status says, in the user's words. */
export const APPLICATION_STATES: Record<ApplicationStatus, Badge> = {
  applied: { label: 'Applied', tone: 'info' },
  interviewing: { label: 'Interviewing', tone: 'info' },
  offer: { label: 'Offer', tone: 'ok' },
  rejected: { label: 'Rejected', tone: 'neutral' },
  withdrawn: { label: 'Withdrawn', tone: 'neutral' },
}

export const APPLICATION_STATUSES = Object.keys(APPLICATION_STATES) as ApplicationStatus[]

/** Today on the user's own calendar, as the tracker records a day. */
export function today(now: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/** A recorded day, the way the page names a day. A bare date is a day
 * on the user's calendar, not midnight UTC — parsed as UTC, it would
 * show as the day before anywhere west of Greenwich. */
export function dayLabel(day: string): string {
  const [y, m, d] = day.split('-').map(Number)
  const at = new Date(y ?? NaN, (m ?? NaN) - 1, d ?? NaN)
  return Number.isNaN(at.getTime())
    ? day
    : at.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** What the user changed in a package's status form. A field they set
 * stays theirs until it is saved and the tracker read back; a field they
 * left follows the tracker. */
export interface TrackerDraft {
  status?: ApplicationStatus | 'cleared'
  day?: string
  /** Sent and answered: what the form shows until the tracker is read
   * back, so it never shows the status from before the save. */
  saved?: boolean
  /** Edited while a save of the posting was out: the posting's status
   * as the form showed it then (null, untracked). The fields the user
   * left keep following it, not the status the save brings. */
  basis?: Application | null
}

/** What a package's status form shows, which is what its Save records:
 * the draft's fields, else the posting's status and its day — the one
 * the draft was edited against, if it was edited during a save. A status
 * the user changed is dated `todayIs` unless they gave a day. */
export function formValues(
  draft: TrackerDraft | undefined,
  current: Application | undefined,
  todayIs: string,
): { status: ApplicationStatus | 'cleared'; day: string } {
  const base = draft?.basis === undefined ? current : (draft.basis ?? undefined)
  const status = draft?.status ?? base?.status ?? 'applied'
  const day = draft?.day ?? (base && status === base.status ? base.occurred_on : todayIs)
  return { status, day }
}

/** Whether a draft shows anything but the posting's own status and day.
 * One that doesn't is no edit: changed and changed back, the form
 * follows the tracker again. Untracked, the draft is all there is. */
export function changes(
  draft: TrackerDraft,
  current: Application | undefined,
  todayIs: string,
): boolean {
  const shown = formValues(draft, current, todayIs)
  return current === undefined || shown.status !== current.status ||
    shown.day !== current.occurred_on
}

/** The drafts still the user's: the saved ones are dropped once the
 * tracker has been read back. The same object when none was saved. */
export function unsaved(drafts: Record<number, TrackerDraft>): Record<number, TrackerDraft> {
  const kept = Object.entries(drafts).filter(([, draft]) => !draft.saved)
  return kept.length === Object.keys(drafts).length ? drafts : Object.fromEntries(kept)
}

/** The drafts once the saves on their postings have settled. A form the
 * user edited while one was out is judged by what the tracker now says,
 * as any edit is: one that shows just the posting's status and day is no
 * edit. `forms` are those forms, as (package id, its posting). The same
 * object when none is dropped. */
export function settled(
  drafts: Record<number, TrackerDraft>,
  forms: ReadonlyArray<readonly [number, string]>,
  applications: ReadonlyMap<string, Application>,
  todayIs: string,
): Record<number, TrackerDraft> {
  const clean = forms.filter(([id, job]) => {
    const draft = drafts[id]
    return draft !== undefined && !changes(draft, applications.get(job), todayIs)
  })
  if (clean.length === 0) return drafts
  const next = { ...drafts }
  for (const [id] of clean) delete next[id]
  return next
}

/** The postings a row stands for: its own and every other listing. */
export function roleIds(row: RoleRow): string[] {
  return [row.job.job_id, ...row.listings.map((l) => l.member.job_id)]
}

/** What a row says of the role's applications: the latest event across
 * its postings that still tracks one — each posting's own status first,
 * so clearing one listing leaves another's intact. `listing` is set when
 * that status is another listing's, not the row's own posting's. */
export function roleApplication(
  row: RoleRow,
  applications: ReadonlyMap<string, Application>,
): { application: Application; listing: GroupMember | null } | null {
  let best: Application | null = null
  for (const id of roleIds(row)) {
    const found = applications.get(id)
    if (found && (best === null || found.event_id > best.event_id)) best = found
  }
  if (best === null) return null
  const latest = best
  return {
    application: latest,
    listing: latest.job_id === row.job.job_id
      ? null
      : (row.listings.find((l) => l.member.job_id === latest.job_id)?.member ?? null),
  }
}
