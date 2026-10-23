import type { Package } from './api'

const FINISHED = new Set(['done', 'failed'])

/** The packages the page last showed before it was hidden, by id: whether
 * each had finished. */
export type Baseline = ReadonlyMap<number, boolean>

export function baselineOf(packages: readonly Package[]): Baseline {
  return new Map(packages.map((p) => [p.id, FINISHED.has(p.status)]))
}

/** How many packages have finished since the baseline: done or failed
 * now, and not finished in it, or not in it at all. Counted by id, so a
 * package read twice counts once. */
export function finishedSince(baseline: Baseline, packages: readonly Package[]): number {
  return new Set(
    packages.filter((p) => FINISHED.has(p.status) && baseline.get(p.id) !== true).map((p) => p.id),
  ).size
}

/** The tab's title. `atLeast` when the baseline is the first read made
 * while hidden, not what the page showed: what finished before that read
 * is not in the count. Nothing finished shows no count at all. */
export function titleFor(finished: number, atLeast: boolean): string {
  return finished > 0 ? `(${finished}${atLeast ? '+' : ''}) Job Agent` : 'Job Agent'
}
