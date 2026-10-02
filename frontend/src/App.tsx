import { useCallback, useEffect, useRef, useState } from 'react'
import type { MouseEvent } from 'react'
import { api } from './api'
import type { EnginesView, LinkedInStatus } from './api'
import { engineBadge, modelLabel, selectedEngine } from './engineStatus'
import { useFocusLanding } from './focus'
import { episodeShown, markEpisodeShown } from './shownEpisodes'
import { chip, column } from './styles'
import { TABS, tabFrom, tabHref } from './tabs'
import type { TabId } from './tabs'
import { usePreference } from './theme'
import { useEpochEffect, useEventStream } from './useEventStream'
import { useLatestOnly } from './useLatestOnly'
import type { ReadOptions } from './useLatestOnly'
import RecoveryBanners from './RecoveryBanners'
import Onboarding from './pages/Onboarding'
import Search from './pages/Search'
import Settings from './pages/Settings'

const STATUS_POLL_MS = 20_000

export default function App() {
  // All engine state — the selection, both engines, their model lists —
  // is read here and only here; Settings and the banners are handed it.
  const [engines, setEngines] = useState<EnginesView | null>(null)
  const engine = engines ? selectedEngine(engines) : null
  const [linkedin, setLinkedin] = useState<LinkedInStatus | null>(null)
  // The address owns the tab: a link to one opens it, and Back and
  // Forward move between them. This state only follows it — every change
  // goes through `setTab`, which writes the address first, or arrives as
  // a history step.
  const [tab, setTabShown] = useState<TabId>(() => tabFrom(window.location.search))
  const setTab = useCallback((id: TabId) => {
    if (tabFrom(window.location.search) !== id) {
      window.history.pushState(null, '', tabHref(window.location.href, id))
    }
    setTabShown(id)
  }, [])
  useEffect(() => {
    const follow = () => setTabShown(tabFrom(window.location.search))
    window.addEventListener('popstate', follow)
    return () => window.removeEventListener('popstate', follow)
  }, [])
  // Whether the page on screen has work the user would lose if it went
  // away: unsaved edits, or an action of theirs still in flight. Reported
  // by the pages themselves.
  const [hold, setHold] = useState(false)
  const engineHeadingRef = useRef<HTMLHeadingElement | null>(null)
  const land = useFocusLanding()
  // The theme preference lives here, not in Settings, because it has to
  // outlive Settings: the listener that follows other tabs must be up
  // while any tab is showing, and with storage blocked this state is the
  // only record of the choice — it would be lost with the page.
  const [appearance, setAppearance] = usePreference()
  // One stream for the whole app. It says which surface went stale; the
  // endpoints below stay the only source of what is actually true.
  const { epochs, connected } = useEventStream()

  // One ticket sequence per endpoint: an event and a reconciliation poll
  // can be in flight together, and the older answer must not land last.
  const readStatus = useLatestOnly()
  const readLinkedin = useLatestOnly()

  const loadEngine = useCallback(
    (options?: ReadOptions) =>
      readStatus(() => api.engines(), setEngines, options).catch(() => setEngines(null)),
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

  // A usage limit opens the engine picker, once per limit: the way to
  // keep working now is another provider's model, while queued work still
  // resumes on its own when the limit lifts. Never over work in progress —
  // switching tabs would throw away unsaved edits or an answer still on
  // its way — so then the banner, which says the same, is the way there.
  const openEngine = useCallback(() => {
    setTab('settings')
    land(engineHeadingRef)
  }, [land, setTab])
  const episode = engine?.status === 'waiting_usage_exhausted' ? engine.episode : null
  useEffect(() => {
    if (!episode || episodeShown(episode)) return
    markEpisodeShown(episode)
    if (tab !== 'settings' && !hold) openEngine()
  }, [episode, tab, hold, openEngine])

  const badge = engine ? engineBadge(engine) : null
  const model = engine ? modelLabel(engines, engine.provider, engine.model) : null

  // A plain click switches in place; one that asks for a new tab or
  // window, or a middle click, is the browser's to follow.
  const follow = (id: TabId) => (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    setTab(id)
  }

  return (
    <div className="min-h-screen bg-bg text-ink">
      {/* One column: the header, the notices, the tabs and the page all
          start at the same left edge, whatever the window width. */}
      <header className="border-b border-line bg-surface">
        <div className={`${column} flex flex-wrap items-center justify-between gap-3 py-4`}>
          <div>
            <h1 className="text-xl font-semibold">Job Agent</h1>
            <p className="text-sm text-ink-3">
              LinkedIn job search with tailored resume + cover letter packages
            </p>
          </div>
          {badge && (
            <span
              className={`rounded-full px-3 py-1 text-sm font-medium ${chip[badge.tone]}`}
              title={engine?.last_error ?? undefined}
            >
              {badge.label}
              {model ? ` · ${model}` : ''}
            </span>
          )}
        </div>
      </header>
      <RecoveryBanners
        engines={engines}
        linkedin={linkedin}
        onOpenEngine={openEngine}
        onOpenSearch={() => setTab('search')}
      />
      <nav aria-label="Pages" className="border-b border-line bg-surface">
        <div className={`${column} flex gap-6`}>
          {TABS.map((t) => (
            <a
              key={t.id}
              href={tabHref(window.location.href, t.id)}
              aria-current={tab === t.id ? 'page' : undefined}
              className={`border-b-2 py-3 text-sm font-medium ${
                tab === t.id
                  ? 'border-accent text-ink'
                  : 'border-transparent text-ink-3 hover:text-ink'
              }`}
              onClick={follow(t.id)}
            >
              {t.label}
            </a>
          ))}
        </div>
      </nav>
      <main className={`${column} py-4 sm:py-8`}>
        {tab === 'profile' ? (
          <Onboarding
            profileEpoch={epochs.profile}
            engineProvider={engine?.provider ?? null}
            onHold={setHold}
          />
        ) : tab === 'search' ? (
          <Search epochs={epochs} connected={connected} onHold={setHold} />
        ) : (
          <Settings
            engines={engines}
            refreshEngine={loadEngine}
            headingRef={engineHeadingRef}
            appearance={appearance}
            setAppearance={setAppearance}
          />
        )}
      </main>
    </div>
  )
}
