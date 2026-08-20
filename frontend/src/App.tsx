import { useEffect, useState } from 'react'
import { api } from './api'
import type { EngineStatus, LinkedInStatus } from './api'
import { engineBadge } from './engineStatus'
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

  useEffect(() => {
    let alive = true
    const poll = () => {
      api
        .status()
        .then((s) => alive && setEngine(s.engine))
        .catch(() => alive && setEngine(null))
      api
        .linkedinStatus()
        .then((s) => alive && setLinkedin(s))
        .catch(() => alive && setLinkedin(null))
    }
    poll()
    const timer = setInterval(poll, STATUS_POLL_MS)
    return () => {
      alive = false
      clearInterval(timer)
    }
  }, [])

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
        {tab === 'profile' ? <Onboarding /> : tab === 'search' ? <Search /> : <Settings />}
      </main>
    </div>
  )
}
