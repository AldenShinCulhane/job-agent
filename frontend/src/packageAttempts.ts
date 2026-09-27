import type { Package } from './api'

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
