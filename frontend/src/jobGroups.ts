import type { GroupMember, Job } from './api'

/** One other listing of a row's role. `job` is set when it is in these
 * results and passed the filters: only then can it be selected. */
export interface Listing {
  member: GroupMember
  job: Job | null
}

/** A row of the job list: one role. */
export interface RoleRow {
  /** The posting the row shows and its checkbox selects. */
  job: Job
  /** The role's other listings, oldest first: this search's and earlier ones. */
  listings: Listing[]
}

/** The list as rows, one per role. `passing` is every posting in these
 * results that passed the filters — filters judge postings, not rows —
 * sorted; a role takes the place of its first posting in that order.
 * A role's row stands for one of its passing postings: one with a
 * package of its own, then the newest. Never the selection: ticking a
 * listing must not swap the row out from under the click. Selection
 * stays a set of posting ids, so a change of groups never changes what
 * is selected; a row says when it folds a selected listing away. */
export function roleRows(
  passing: Job[],
  hasPackage: (jobId: string) => boolean,
): RoleRow[] {
  const byGroup = new Map<string, Job[]>()
  const order: (Job | string)[] = []
  for (const job of passing) {
    const id = job.group?.id
    if (!id) {
      order.push(job)
      continue
    }
    const members = byGroup.get(id)
    if (members) members.push(job)
    else {
      byGroup.set(id, [job])
      order.push(id)
    }
  }
  const rank = (job: Job): [number, number] => [
    hasPackage(job.job_id) ? 0 : 1,
    -Date.parse(job.first_seen_at) || 0,
  ]
  const before = (a: Job, b: Job) => {
    const [a0, a1] = rank(a)
    const [b0, b1] = rank(b)
    return a0 - b0 || a1 - b1 || (a.job_id < b.job_id ? -1 : a.job_id > b.job_id ? 1 : 0)
  }
  return order.map((entry) => {
    if (typeof entry !== 'string') return { job: entry, listings: [] }
    const members = byGroup.get(entry)!
    const job = members.reduce((best, m) => (before(m, best) < 0 ? m : best))
    const shown = new Map(members.map((m) => [m.job_id, m]))
    const listings = (job.group?.members ?? [])
      .filter((m) => m.job_id !== job.job_id)
      .map((member) => ({ member, job: (member.in_run && shown.get(member.job_id)) || null }))
    return { job, listings }
  })
}

/** Every posting the rows let the user select: each row's own, and the
 * listings in these results that passed the filters. */
export function selectableIds(rows: RoleRow[]): string[] {
  return rows.flatMap((row) => [
    row.job.job_id,
    ...row.listings.filter((l) => l.job !== null).map((l) => l.member.job_id),
  ])
}

/** The role a posting is a listing of: its group, or itself alone. */
const roleOf = (job: Job) => job.group?.id ?? `posting:${job.job_id}`

/** How many roles these postings are. */
export function roleCount(jobs: Job[]): number {
  return new Set(jobs.map(roleOf)).size
}

/** How many of those roles are new. Every listing of a role in the
 * results is new or none is, since being new looks at all of them. */
export function newRoleCount(jobs: Job[]): number {
  return new Set(jobs.filter((j) => j.novelty === 'new').map(roleOf)).size
}

/** A listing's first-seen day, the way the list names a listing. */
export function listedOn(iso: string): string {
  const at = Date.parse(iso)
  return Number.isNaN(at) ? 'on an unknown day'
    : new Date(at).toLocaleDateString([], { month: 'short', day: 'numeric' })
}
