import type { Package, UnsupportedClaim } from './api'

/** The ids of each job's newest package, from a newest-first list. Only
 * those offer Try again or Regenerate: an older row is history, and a
 * click on it would start from inputs a newer package already moved past. */
export function newestPerJob(packages: Package[]): Set<number> {
  const seen = new Set<string>()
  const newest = new Set<number>()
  for (const pkg of packages) {
    if (seen.has(pkg.job_id)) continue
    seen.add(pkg.job_id)
    newest.add(pkg.id)
  }
  return newest
}

/** Each job's newest package, from a newest-first list. */
export function newestByJob(packages: Package[]): Map<string, Package> {
  const newest = new Map<string, Package>()
  for (const pkg of packages) if (!newest.has(pkg.job_id)) newest.set(pkg.job_id, pkg)
  return newest
}

/** The list as it is shown: newest first, except that a failed package
 * still waiting on the user — its job's newest, so Try again is on it —
 * comes before everything else. A failure a later attempt answered is
 * history and keeps its place. */
export function failedFirst(packages: Package[], newest: Set<number>): Package[] {
  const open = (pkg: Package) => pkg.status === 'failed' && newest.has(pkg.id)
  return [...packages.filter(open), ...packages.filter((pkg) => !open(pkg))]
}

/** What stopped a package at the truth check, counted. Only claims the
 * profile doesn't support are called that; a structure failure, alone or
 * among them, makes the count neutral. */
export function truthCheckSummary(failures: UnsupportedClaim[]): string {
  const n = failures.length
  if (failures.every((f) => f.kind === 'claim')) {
    return n === 1 ? 'a claim your profile doesn’t support'
      : `${n} claims your profile doesn’t support`
  }
  return n === 1 ? 'a validation issue' : `${n} validation issues`
}

/** The button a package offers, if any. */
export function retryAction(pkg: Package, newest: Set<number>): 'Try again' | 'Regenerate' | null {
  if (!newest.has(pkg.id)) return null
  if (pkg.status === 'failed') return 'Try again'
  if (pkg.status === 'done') return 'Regenerate'
  return null
}

function listText(items: string[]): string {
  if (items.length <= 1) return items.join('')
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`
}

/** How a row a retry made says what it is: another attempt of the same
 * inputs, or a new package because inputs changed — never presented as
 * another attempt of the old one. Null for a row the user selected. */
export function attemptLabel(pkg: Package): string | null {
  if (pkg.retry_of === null) return null
  if (pkg.inputs_changed.length > 0) {
    return `New package: the ${listText(pkg.inputs_changed)} changed since the one you retried`
  }
  return `Attempt ${pkg.attempt}`
}

/** A fresh key for one click. */
export function requestKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}
