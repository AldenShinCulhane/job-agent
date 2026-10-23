import { useState } from 'react'

// How often a slice that is owed a refresh, but whose last read failed,
// tries again. Only ever fires while something is actually owed.
export const OWED_RETRY_MS = 5_000

export interface ReadOptions {
  /** Issue this read only if the newest read of this state has already
   * resolved. For TIMER callers — a tick proves that time passed, not
   * that anything changed, so it has no business cancelling a response
   * that is still on its way. Left unset for callers that KNOW something
   * moved (an event, a completed mutation): those supersede. */
  ifIdle?: boolean
}

export type ReadLatest = <T>(
  fetcher: () => Promise<T>,
  apply: (value: T) => void,
  options?: ReadOptions,
) => Promise<boolean>

/** Apply only the newest read of one endpoint.
 *
 * Refetches of the same endpoint overlap routinely — a mount fetch and
 * the stream's first-open invalidation, an event landing during a poll,
 * two events a moment apart. Nothing makes responses come back in the
 * order they were sent, so without this the slower older response lands
 * last and writes older data over newer (a version counting *backwards*
 * on screen, with no local edit involved).
 *
 * Each call takes a ticket; a response whose ticket has been superseded
 * is dropped before it can write — a REJECTION included. A stale failure
 * is as wrong as stale data: letting it reach the caller's error handler
 * lets a request that is already out of date clear state a newer, good
 * response just wrote (an engine reset to null takes the recovery banner
 * off the screen with it).
 *
 * One instance per piece of state, written by ONE endpoint. Two rules,
 * learned the hard way in both directions:
 *
 *  - Two writers of the same state that do NOT share a sequence are not
 *    ordered against each other at all, so the older answer can land last.
 *  - Two writers that DO share one can cancel each other — and a writer
 *    that only patches a field cannot stand in for one that initialises
 *    the whole object. A small engine refresh superseding a full settings
 *    load left that page on "Loading…" permanently.
 *
 * When both apply, the fix is to stop sharing the state: give each piece
 * a single endpoint that owns it.
 *
 * A third rule, about WHO may supersede. Superseding is right when the
 * caller knows something changed since the pending read went out. A
 * retry timer knows no such thing, and a timer shorter than the round
 * trip then cancels every response before it lands: each tick takes a
 * newer ticket, each reply arrives to find itself stale, nothing ever
 * applies, and the retry runs forever — ten successful responses, zero
 * adoptions. So a paced caller passes `ifIdle` and stands down while the
 * newest read is still out; only a prompted caller supersedes.
 *
 * Resolves TRUE only when `apply` actually ran. A caller tracking whether
 * a slice is still owed a refresh must not clear that on a read it threw
 * away: a superseded response resolves false, and the newer read is the
 * one whose outcome counts. A read skipped by `ifIdle` resolves false
 * too — the pending read is the one that will settle it.
 */
export function latestOnly(): ReadLatest {
  let issued = 0
  // Whether the NEWEST ticket is still unresolved. An older one that is
  // still out does not count: its answer is already spoken for.
  let pending = false
  return async <T>(
    fetcher: () => Promise<T>,
    apply: (value: T) => void,
    options?: ReadOptions,
  ): Promise<boolean> => {
    if (options?.ifIdle && pending) return false
    const ticket = ++issued
    pending = true
    let value: T
    try {
      value = await fetcher()
    } catch (err) {
      if (ticket !== issued) return false // stale failure, stale news
      pending = false
      throw err
    }
    if (ticket !== issued) return false // a newer read owns this
    pending = false
    apply(value)
    return true
  }
}

/** One `latestOnly` sequence for the lifetime of the component. */
export function useLatestOnly(): ReadLatest {
  const [read] = useState(latestOnly)
  return read
}
