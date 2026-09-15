import { useCallback, useEffect, useState } from 'react'
import { api } from './api'
import type { EngineStatus, LinkedInStatus } from './api'
import { engineBadge } from './engineStatus'
import { useEpochEffect, useEventStream } from './useEventStream'
import { useLatestOnly } from './useLatestOnly'
import type { ReadOptions } from './useLatestOnly'
import RecoveryBanners from './RecoveryBanners'
import Onboarding from './pages/Onboarding'
import Search from './pages/Search'
import Settings from './pages/Settings'

const TABS = [
  { id: 'profile', label: 'Profile' },
  { id: 'search', label: 'Search' },
  { id: 'settings', label: 'Settings' },
] as const

type TabId = (typeof TABS)[number]['id']

const STATUS_POLL_MS = 20_000

export default function App() {
  const [engine, setEngine] = useState<EngineStatus | null>(null)
  const [linkedin, setLinkedin] = useState<LinkedInStatus | null>(null)
  const [tab, setTab] = useState<TabId>('profile')
  // One stream for the whole app. It says which surface went stale; the
  // endpoints below stay the only source of what is actually true.
  const { epochs, connected } = useEventStream()

  // One ticket sequence per endpoint: an event and a reconciliation poll
  // can be in flight together, and the older answer must not land last.
  const readStatus = useLatestOnly()
  const readLinkedin = useLatestOnly()

  const loadEngine = useCallback(
    (options?: ReadOptions) =>
      readStatus(
        () => api.status(),
        (s) => setEngine(s.engine),
        options,
      ).catch(() => setEngine(null)),
    [readStatus],
  )
  const loadLinkedin = useCallback(
    (options?: ReadOptions) =>
      readLinkedin(() => api.linkedinStatus(), setLinkedin, options).catch(
        () => setLinkedin(null),
      ),
    [readLinkedin],
  )

  useEffect(() => {
    void loadEngine()
    void loadLinkedin()
  }, [loadEngine, loadLinkedin])

  useEpochEffect(epochs.engine, loadEngine)
  useEpochEffect(epochs.linkedin, loadLinkedin)

  // Reconciliation, not the primary path. Three things the stream cannot
  // cover: a disconnect (nothing is replayed on reconnect, and the gap is
  // invisible until then); the usage-limit countdown, which moves with the
  // clock and writes no further state to announce; and a read that simply
  // failed, leaving state null — an unknown status is precisely when
  // reconciliation is owed, and no event is coming to prompt it.
  // `ifIdle`: a tick never cancels a read still in flight — a timer
  // shorter than the round trip would otherwise supersede every answer
  // before it landed. Events and the mount fetch supersede as before.
  const counting = engine?.status === 'waiting_usage_exhausted'
  const unknown = engine === null || linkedin === null
  useEffect(() => {
    if (connected && !counting && !unknown) return
    const timer = setInterval(() => {
      void loadEngine({ ifIdle: true })
      void loadLinkedin({ ifIdle: true })
    }, STATUS_POLL_MS)
    return () => clearInterval(timer)
  }, [connected, counting, unknown, loadEngine, loadLinkedin])

  const badge = engine ? engineBadge(engine.status) : null

  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-8 py-4">
        <div>
          <h1 className="text-xl font-semibold">Job Agent</h1>
          <p className="text-sm text-slate-500">
            LinkedIn job search with tailored resume + cover letter packages
          </p>
        </div>
        {badge && (
          <span
            className={`rounded-full px-3 py-1 text-sm font-medium ${badge.tone}`}
            title={engine?.last_error ?? undefined}
          >
            {badge.label}
            {engine?.model ? ` · ${engine.model}` : ''}
          </span>
        )}
      </header>
      <RecoveryBanners
        engine={engine}
        linkedin={linkedin}
        onOpenSettings={() => setTab('settings')}
        onOpenSearch={() => setTab('search')}
      />
      <nav className="border-b border-slate-200 bg-white px-8">
        <div className="mx-auto flex max-w-7xl gap-6">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`border-b-2 py-3 text-sm font-medium ${
                tab === t.id
                  ? 'border-slate-900 text-slate-900'
                  : 'border-transparent text-slate-500 hover:text-slate-800'
              }`}
              onClick={() => setTab(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
      </nav>
      <main className="mx-auto max-w-7xl p-8">
        {tab === 'profile' ? (
          <Onboarding profileEpoch={epochs.profile} />
        ) : tab === 'search' ? (
          <Search epochs={epochs} connected={connected} />
        ) : (
          <Settings engine={engine} refreshEngine={loadEngine} />
        )}
      </main>
    </div>
  )
}
