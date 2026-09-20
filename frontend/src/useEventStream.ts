import { useEffect, useRef, useState } from 'react'

/** Every topic the backend publishes on. A topic names the surface that
 * went stale, never the change itself — see the invalidation note below. */
export const TOPICS = [
  'engine',
  'linkedin',
  'search',
  'scoring',
  'packages',
  'profile',
] as const

export type Topic = (typeof TOPICS)[number]

/** A counter per topic. It bumps when that topic's data changed; the
 * value carries no meaning beyond "different from last time". */
export type Epochs = Record<Topic, number>

export interface EventStream {
  epochs: Epochs
  /** False while the stream is down. Polling widens to cover the gap —
   * events that happen while disconnected are simply lost, there is no
   * replay. Comes from onopen/onerror only; the server's heartbeat is a
   * comment, which EventSource never surfaces. */
  connected: boolean
}

const ZERO: Epochs = {
  engine: 0,
  linkedin: 0,
  search: 0,
  scoring: 0,
  packages: 0,
  profile: 0,
}

// A scoring run publishes once per job and a generation batch once per
// package transition. Coalescing a burst into one bump keeps that from
// becoming one refetch per event.
const DEBOUNCE_MS = 200

// EventSource retries a dropped connection by itself, but a non-200
// answer — a server mid-restart, a proxy's 502 page — makes it give up
// for good (readyState CLOSED, no further attempts). The stream is then
// gone for the life of the page while polling quietly covers at its
// slowest rate. So a closed source is replaced, after a pause.
const REOPEN_MS = 5_000

/** One EventSource for the whole app.
 *
 * The stream is INVALIDATION, NOT TRUTH: the payloads are ignored
 * entirely. An event means "this topic's endpoint may now say something
 * different" — the consumer refetches the durable endpoint and believes
 * that. Nothing is reconstructed from the event, so a dropped, doubled,
 * or out-of-order event costs at most one redundant fetch.
 */
export function useEventStream(): EventStream {
  const [epochs, setEpochs] = useState<Epochs>(ZERO)
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    const pending = new Set<Topic>()
    let timer: ReturnType<typeof setTimeout> | null = null

    const flush = () => {
      timer = null
      if (pending.size === 0) return
      const topics = Array.from(pending)
      pending.clear()
      setEpochs((prev) => {
        const next = { ...prev }
        for (const topic of topics) next[topic] = next[topic] + 1
        return next
      })
    }

    const bump = (...topics: Topic[]) => {
      for (const topic of topics) pending.add(topic)
      if (timer === null) timer = setTimeout(flush, DEBOUNCE_MS)
    }

    let source: EventSource
    let reopen: ReturnType<typeof setTimeout> | null = null
    const handlers = TOPICS.map((topic) => ({ topic, handler: () => bump(topic) }))

    const open = () => {
      reopen = null
      source = new EventSource('/api/events')
      source.onopen = () => {
        setConnected(true)
        // Every open, the first included. Before the first one there is
        // a window between the consumers' initial fetch and the
        // subscription existing; a change landing in it is announced to
        // nobody, and once `connected` turns off idle polling nothing
        // would ever go back for it. A reconnect is the same hole,
        // reopened. Nothing is redelivered either way, so every surface
        // is stale until refetched.
        bump(...TOPICS)
      }
      source.onerror = () => {
        setConnected(false)
        // A dropped connection: EventSource is already retrying. A
        // refused one: it has stopped, and only a new source will try.
        if (source.readyState === EventSource.CLOSED && reopen === null) {
          reopen = setTimeout(open, REOPEN_MS)
        }
      }
      // Named events never arrive through onmessage — each topic needs
      // its own listener.
      for (const { topic, handler } of handlers) source.addEventListener(topic, handler)
    }
    open()

    // A backgrounded tab can have its stream throttled or torn down with
    // no error surfaced, so returning to it re-reads everything.
    const onVisible = () => {
      if (document.visibilityState === 'visible') bump(...TOPICS)
    }
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      if (timer !== null) clearTimeout(timer)
      if (reopen !== null) clearTimeout(reopen)
      document.removeEventListener('visibilitychange', onVisible)
      for (const { topic, handler } of handlers) {
        source.removeEventListener(topic, handler)
      }
      source.close()
    }
  }, [])

  return { epochs, connected }
}

/** Run `fn` when `epoch` moves, but not on mount.
 *
 * Consumers do their own initial fetch; firing here on mount too would
 * double every page load. `fn` is read through a ref so a fresh closure
 * each render doesn't re-trigger the effect.
 */
export function useEpochEffect(epoch: number, fn: () => void): void {
  const latest = useRef(fn)
  latest.current = fn
  const seen = useRef(epoch)

  useEffect(() => {
    if (seen.current === epoch) return
    seen.current = epoch
    latest.current()
  }, [epoch])
}
