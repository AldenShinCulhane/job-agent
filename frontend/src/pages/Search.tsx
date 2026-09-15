import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { api, packageFileUrl } from '../api'
import type { Explanation, Job, LinkedInStatus, Package, SearchRun } from '../api'
import { useEpochEffect } from '../useEventStream'
import type { Epochs } from '../useEventStream'
import { useLatestOnly } from '../useLatestOnly'
import type { ReadOptions } from '../useLatestOnly'

const inputCls =
  'w-full rounded-md border border-slate-300 px-2 py-1 text-sm focus:border-slate-500 focus:outline-none'
const labelCls = 'block text-xs font-medium uppercase tracking-wide text-slate-500'

const LINKEDIN_STATES: Record<string, { label: string; tone: string }> = {
  idle: { label: 'Not connected', tone: 'bg-slate-100 text-slate-600' },
  connecting: { label: 'Connecting…', tone: 'bg-sky-100 text-sky-800' },
  ready: { label: 'LinkedIn connected', tone: 'bg-emerald-100 text-emerald-800' },
  logged_out: { label: 'Sign-in needed', tone: 'bg-amber-100 text-amber-800' },
  captcha_stop: { label: 'Checkpoint — stopped', tone: 'bg-rose-100 text-rose-800' },
  error: { label: 'Browser error', tone: 'bg-rose-100 text-rose-800' },
}

/** The four endpoints an event can invalidate on this page. */
type Slice = 'linkedin' | 'run' | 'jobs' | 'packages' | 'profile'

const NOTHING_OWED: Record<Slice, boolean> = {
  linkedin: false,
  run: false,
  jobs: false,
  packages: false,
  profile: false,
}

// How often a slice that is owed a refresh, but whose last read failed,
// tries again. Only ever fires while something is actually owed.
const OWED_RETRY_MS = 5_000

const RUN_LABELS: Record<string, string> = {
  running: 'Searching…',
  done: 'Search complete',
  stopped_captcha: 'Stopped at a LinkedIn checkpoint — resumes after you resolve it and reconnect',
  stopped_auth: 'Stopped — sign in via Connect and the search resumes automatically',
  stopped_cap: 'Daily action cap reached — resumes automatically when it resets',
  stopped_error: 'Stopped on an error',
  superseded: 'Replaced by a newer search',
}

const PACKAGE_STATES: Record<string, { label: string; tone: string }> = {
  queued: { label: 'Queued', tone: 'bg-slate-100 text-slate-600' },
  generating: { label: 'Generating…', tone: 'bg-sky-100 text-sky-800' },
  validating: { label: 'Validating…', tone: 'bg-sky-100 text-sky-800' },
  fitting: { label: 'Fitting pages…', tone: 'bg-sky-100 text-sky-800' },
  asserting: { label: 'Final checks…', tone: 'bg-sky-100 text-sky-800' },
  publishing: { label: 'Publishing…', tone: 'bg-sky-100 text-sky-800' },
  done: { label: 'Ready', tone: 'bg-emerald-100 text-emerald-800' },
  failed: { label: 'Failed', tone: 'bg-rose-100 text-rose-800' },
  waiting_usage: {
    label: 'Waiting — usage limit',
    tone: 'bg-amber-100 text-amber-800',
  },
}

const PACKAGE_ACTIVE = new Set([
  'queued', 'generating', 'validating', 'fitting', 'asserting', 'publishing',
])

const WORKPLACE_OPTIONS = ['remote', 'hybrid', 'onsite'] as const

const EXPERIENCE_OPTIONS = [
  { id: 'internship', label: 'Internship' },
  { id: 'entry', label: 'Entry level' },
  { id: 'associate', label: 'Associate' },
  { id: 'mid_senior', label: 'Mid-Senior' },
  { id: 'director', label: 'Director' },
  { id: 'executive', label: 'Executive' },
] as const

// Tab on an empty field accepts its placeholder suggestion; focus stays
// put so the accepted value is visible, and the next Tab moves on.
const acceptPlaceholderOnTab =
  (setter: (value: string) => void) => (e: KeyboardEvent<HTMLInputElement>) => {
    const el = e.currentTarget
    if (e.key === 'Tab' && !e.shiftKey && el.value === '' && el.placeholder) {
      e.preventDefault()
      setter(el.placeholder)
    }
  }

export default function Search({
  epochs,
  connected,
}: {
  epochs: Epochs
  connected: boolean
}) {
  const [linkedin, setLinkedin] = useState<LinkedInStatus | null>(null)
  const [run, setRun] = useState<SearchRun | null>(null)
  const [jobs, setJobs] = useState<Job[]>([])
  const [scoringPending, setScoringPending] = useState(0)
  const [scoringStall, setScoringStall] = useState<{
    reason: string
    detail: string | null
  } | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [busyDetail, setBusyDetail] = useState<string | null>(null)
  const [busyExplain, setBusyExplain] = useState<string | null>(null)
  const [busyRerank, setBusyRerank] = useState(false)
  const [explanations, setExplanations] = useState<Record<string, Explanation>>({})
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [packages, setPackages] = useState<Package[]>([])
  const [busyGenerate, setBusyGenerate] = useState(false)
  const [sortBy, setSortBy] = useState<'score' | 'date'>('score')
  const [hideExceeds, setHideExceeds] = useState(false)
  const [easyOnly, setEasyOnly] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [profileLocation, setProfileLocation] = useState<string | null>(null)

  const [keywords, setKeywords] = useState('')
  const [location, setLocation] = useState('')
  const [datePosted, setDatePosted] = useState('')
  const [workplace, setWorkplace] = useState<string[]>([])
  const [experience, setExperience] = useState<string[]>([])
  const [cap, setCap] = useState(100)
  // Which run the rows ON SCREEN came from. Two copies on purpose: the
  // ref is read synchronously inside the response handler (a useCallback
  // closure cannot see current state), the state is what the render needs
  // — while it disagrees with `run`, the rows belong to a result set that
  // has already been superseded.
  const jobsRunIdRef = useRef<number | null>(null)
  const [jobsRunId, setJobsRunId] = useState<number | null>(null)

  // One refetcher per topic the event stream can invalidate. Each is the
  // single place its slice of state is written, so an event refetches
  // exactly the endpoint that owns what changed and nothing else.
  // One ticket sequence per endpoint — an event refetch and a
  // reconciliation poll overlap constantly, and the older answer must
  // not be the one that lands last.
  const readLinkedin = useLatestOnly()
  const readRun = useLatestOnly()
  const readJobs = useLatestOnly()
  const readPackages = useLatestOnly()
  const readProfile = useLatestOnly()

  // Which slices are known stale and have not been successfully re-read.
  // An event says "this went stale"; if the refetch it triggers FAILS,
  // that fact has been consumed and no second event is coming — the list
  // would sit wrong until an unrelated one arrived. Cleared only by a
  // read that was actually applied, so a superseded response (which owns
  // nothing) neither sets nor clears it.
  const [owed, setOwed] = useState<Record<Slice, boolean>>(NOTHING_OWED)
  const settle = useCallback((slice: Slice, ok: boolean) => {
    setOwed((prev) => (prev[slice] === !ok ? prev : { ...prev, [slice]: !ok }))
  }, [])

  // Each takes the read's `options` so a timer can pass `ifIdle` — see
  // the poll and the owed retry below.
  const refreshLinkedin = useCallback(async (options?: ReadOptions) => {
    try {
      if (await readLinkedin(() => api.linkedinStatus(), setLinkedin, options)) {
        settle('linkedin', true)
      }
    } catch (e) {
      setError(String(e))
      settle('linkedin', false)
    }
  }, [readLinkedin, settle])

  const refreshRun = useCallback(async (options?: ReadOptions) => {
    try {
      const applied = await readRun(
        () => api.searchLatest(),
        (latest) => setRun(latest.id === null ? null : latest),
        options,
      )
      if (applied) settle('run', true)
    } catch (e) {
      setError(String(e))
      settle('run', false)
    }
  }, [readRun, settle])

  const refreshJobs = useCallback(async (options?: ReadOptions) => {
    try {
      const applied = await readJobs(
        () => api.jobs(),
        (jobList) => {
          // Another tab can start a search, and the list that arrives is
          // then a different result set. Selections and explanations are
          // keyed to job ids from the run they were made in, so they are
          // dropped the moment the rendered list changes run — checked
          // against the LIST's run, because the rows are what the user
          // clicked and the run row is a separate, unsynchronised fetch.
          if (jobsRunIdRef.current !== null
              && jobList.run_id !== jobsRunIdRef.current) {
            setSelected(new Set())
            setExplanations({})
            setExpanded(null)
          }
          jobsRunIdRef.current = jobList.run_id
          setJobsRunId(jobList.run_id)
          setJobs(jobList.jobs)
          setScoringPending(jobList.scoring_pending ?? 0)
          setScoringStall(jobList.scoring_stall ?? null)
        },
        options,
      )
      if (applied) settle('jobs', true)
    } catch (e) {
      setError(String(e))
      settle('jobs', false)
    }
  }, [readJobs, settle])

  const refreshPackages = useCallback(async (options?: ReadOptions) => {
    try {
      if (await readPackages(() => api.packages(), (list) =>
        setPackages(list.packages), options,
      )) {
        settle('packages', true)
      }
    } catch (e) {
      setError(String(e))
      settle('packages', false)
    }
  }, [readPackages, settle])

  const loadProfileLocation = useCallback(async (options?: ReadOptions) => {
    try {
      const applied = await readProfile(
        () => api.getProfile(),
        (p) => setProfileLocation(p.profile?.contact.location ?? null),
        options,
      )
      if (applied) settle('profile', true)
    } catch {
      // Silent on screen — this only fills a placeholder, and an error
      // banner for it would be noise. Still owed a retry: dropping the
      // failure is what makes a read a dead end, impact aside.
      settle('profile', false)
    }
  }, [readProfile, settle])

  const refresh = useCallback(
    (options?: ReadOptions) =>
      Promise.all([
        refreshLinkedin(options),
        refreshRun(options),
        refreshJobs(options),
        refreshPackages(options),
      ]),
    [refreshLinkedin, refreshRun, refreshJobs, refreshPackages],
  )

  useEffect(() => {
    void refresh()
    void loadProfileLocation()
  }, [refresh, loadProfileLocation])

  useEpochEffect(epochs.linkedin, refreshLinkedin)
  useEpochEffect(epochs.search, () => {
    void refreshRun()
    void refreshJobs() // a run collects jobs as it goes
  })
  useEpochEffect(epochs.scoring, refreshJobs)
  useEpochEffect(epochs.packages, refreshPackages)
  // A confirm or discard changes which scores /api/jobs can show — score
  // rows are keyed to a profile version — and the location suggestion
  // comes from the same profile.
  useEpochEffect(epochs.profile, () => {
    void refreshJobs()
    void loadProfileLocation()
  })

  // Fast poll while a run is active or the session is mid-transition;
  // slow poll while a stop will self-resume (cap reset, auth/captcha
  // recovery) so the UI catches the backend's automatic restart.
  const packagesActive = packages.some((p) => PACKAGE_ACTIVE.has(p.status))
  const fastPoll =
    run?.status === 'running' ||
    (run?.status === 'done' && scoringPending > 0 && !scoringStall) || // pipeline working
    packagesActive || // tailoring batch in flight
    linkedin?.state === 'connecting' ||
    linkedin?.state === 'logged_out' ||
    linkedin?.state === 'captcha_stop'
  const slowPoll =
    !fastPoll &&
    (run?.status === 'stopped_cap' ||
      run?.status === 'stopped_auth' ||
      run?.status === 'stopped_captcha' ||
      packages.some((p) => p.status === 'waiting_usage') || // resumes itself
      (scoringPending > 0 && scoringStall !== null)) // stalled — watch lazily
  // Reconciliation only, once the stream is up: events already carry
  // every change, so an active batch is watched at a fraction of the old
  // rate. A disconnect makes polling the ONLY source again — nothing is
  // replayed on reconnect — so it runs even when nothing is in flight.
  const pollMs = fastPoll
    ? connected
      ? 15_000
      : 2_500
    : slowPoll || !connected
      ? 30_000
      : 0
  // `ifIdle`: a tick that finds the previous read still out stands down.
  // A timer that superseded instead would, with a round trip longer than
  // its interval, cancel every response before it could land — and the
  // 2.5 s disconnected rate is the one most likely to meet a slow server.
  useEffect(() => {
    if (pollMs === 0) return
    const timer = setInterval(() => void refresh({ ifIdle: true }), pollMs)
    return () => clearInterval(timer)
  }, [pollMs, refresh])

  // Retry only what is actually owed. Connected and idle, `pollMs` is 0
  // and no further event is coming for a change already announced, so a
  // read that failed would otherwise leave that slice wrong until an
  // unrelated event, a tab switch, or a remount. Unbounded on purpose:
  // it stops the moment a read lands (success clears the flag and tears
  // this down), and giving up instead would restore exactly the silent
  // staleness being fixed — the same reasoning as the disconnected poll.
  //
  // Single-flight per slice (`ifIdle`): a tick while the last retry is
  // still out issues nothing. Otherwise a read slower than the interval
  // never lands — every tick supersedes the one before, every response
  // arrives stale, the slice stays owed, and the loop runs forever. An
  // EVENT for the slice still supersedes, through `useEpochEffect` above:
  // that caller knows the pending answer is already behind.
  const owedAny =
    owed.linkedin || owed.run || owed.jobs || owed.packages || owed.profile
  useEffect(() => {
    if (!owedAny) return
    const paced: ReadOptions = { ifIdle: true }
    const timer = setInterval(() => {
      if (owed.linkedin) void refreshLinkedin(paced)
      if (owed.run) void refreshRun(paced)
      if (owed.jobs) void refreshJobs(paced)
      if (owed.packages) void refreshPackages(paced)
      if (owed.profile) void loadProfileLocation(paced)
    }, OWED_RETRY_MS)
    return () => clearInterval(timer)
  }, [owedAny, owed, refreshLinkedin, refreshRun, refreshJobs, refreshPackages,
      loadProfileLocation])

  const connect = async () => {
    setError(null)
    try {
      setLinkedin(await api.linkedinConnect())
    } catch (e) {
      setError(String(e))
    }
  }

  const start = async () => {
    setError(null)
    // A new search is a new result set — selections and explanations
    // from the previous run don't carry over.
    setSelected(new Set())
    setExplanations({})
    setExpanded(null)
    try {
      await api.searchStart({
        keywords,
        location: location || null,
        date_posted: datePosted || null,
        workplace,
        experience,
        cap,
      })
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const resume = async () => {
    setError(null)
    try {
      await api.searchResume()
      await refresh()
    } catch (e) {
      setError(String(e))
    }
  }

  const fetchDetail = async (jobId: string) => {
    setBusyDetail(jobId)
    setError(null)
    try {
      const detail = await api.jobDetail(jobId)
      setJobs((all) => all.map((j) => (j.job_id === jobId ? { ...j, ...detail } : j)))
      setExpanded(jobId)
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyDetail(null)
    }
  }

  const explain = async (jobId: string) => {
    setBusyExplain(jobId)
    setError(null)
    try {
      const explanation = await api.jobExplain(jobId)
      setExplanations((all) => ({ ...all, [jobId]: explanation }))
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyExplain(null)
    }
  }

  const rerank = async () => {
    setBusyRerank(true)
    setError(null)
    try {
      await api.searchRerank()
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyRerank(false)
    }
  }

  // The run row and the job list are two unsynchronised fetches, so
  // between them the newest run can be known while run-1 rows are still
  // rendered. Anything selected in that window belongs to a dead result
  // set, and "is it rendered?" cannot tell the difference — the rows are
  // rendered, they are just stale. Selection is frozen until the list
  // catches up.
  const listStale = run !== null && jobsRunId !== null && run.id !== jobsRunId

  // What is BOTH selected and currently on screen. The list can also be
  // replaced under an existing selection, and a job id that is no longer
  // rendered must never be submitted, whichever response won the race.
  const selectedVisible = useMemo(
    () => jobs.filter((j) => selected.has(j.job_id)).map((j) => j.job_id),
    [jobs, selected],
  )

  // Gate 3: generating packages IS the job-selection gate — everything
  // after this click runs unattended.
  const generatePackages = async () => {
    // Belt to the disabled button's braces: a click already dispatched
    // when the run response landed must not get through either.
    if (listStale || selectedVisible.length === 0) return
    setBusyGenerate(true)
    setError(null)
    try {
      // Sent with the run the rows came from — the server rejects ids
      // that are not in it rather than generating for a job the user
      // never saw.
      await api.packagesGenerate(selectedVisible, jobsRunId)
      setSelected(new Set())
      await refresh()
    } catch (e) {
      setError(String(e))
    } finally {
      setBusyGenerate(false)
    }
  }

  const toggleSelected = (jobId: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(jobId)) next.delete(jobId)
      else next.add(jobId)
      return next
    })

  // llm_rerank (when present) refines ordering within its band; the
  // deterministic score stays visible as the backbone.
  const displayScore = (j: Job) => j.llm_rerank ?? j.deterministic_score

  // Posting date when LinkedIn gave a parseable one; the card's
  // first-seen time is only the fallback.
  const postedTime = (j: Job) => {
    const posted = j.posted_text ? Date.parse(j.posted_text) : NaN
    return Number.isNaN(posted) ? Date.parse(j.first_seen_at) || 0 : posted
  }

  const visibleJobs = useMemo(() => {
    let list = jobs
    if (hideExceeds)
      list = list.filter((j) => !j.score_breakdown?.exceeds_experience)
    if (easyOnly) list = list.filter((j) => j.easy_apply)
    if (sortBy === 'score')
      list = [...list].sort((a, b) => {
        const byScore = (displayScore(b) ?? -1) - (displayScore(a) ?? -1)
        if (byScore !== 0) return byScore
        // Equal scores: the LLM's rank ordinal breaks the tie (an
        // all-equal rerank cluster would otherwise lose its order).
        return (a.llm_rank ?? Infinity) - (b.llm_rank ?? Infinity)
      })
    else list = [...list].sort((a, b) => postedTime(b) - postedTime(a))
    return list
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobs, hideExceeds, easyOnly, sortBy])

  const scoredCount = jobs.filter((j) => j.deterministic_score !== null).length

  const badge = linkedin
    ? (LINKEDIN_STATES[linkedin.state] ?? { label: linkedin.state, tone: 'bg-slate-100 text-slate-600' })
    : null

  return (
    <div className="space-y-6">
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">LinkedIn</h2>
          <div className="flex items-center gap-3">
            {badge && (
              <span className={`rounded-full px-3 py-1 text-sm font-medium ${badge.tone}`}>
                {badge.label}
              </span>
            )}
            {linkedin?.state !== 'ready' && (
              <button
                className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
                disabled={linkedin?.state === 'connecting'}
                onClick={connect}
              >
                Connect
              </button>
            )}
          </div>
        </div>
        {linkedin?.detail && <p className="mt-2 text-sm text-amber-700">{linkedin.detail}</p>}
        <p className="mt-2 text-xs text-slate-400">
          A real browser window opens for sign-in; your session stays on this machine.
          Sign in with your LinkedIn email and password — Google blocks its sign-in
          inside automated browsers, so "Continue with Google" won't work here.
          Automated collection is paced but still against LinkedIn's terms — account
          restriction is a risk you accept (see README).
        </p>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-4 text-lg font-semibold">Job search</h2>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <label className="col-span-2 block">
            <span className={labelCls}>Keywords</span>
            <input
              className={inputCls}
              value={keywords}
              onChange={(e) => setKeywords(e.target.value)}
              onKeyDown={acceptPlaceholderOnTab(setKeywords)}
              placeholder="software engineer"
            />
          </label>
          <label className="block">
            <span className={labelCls}>Location</span>
            <input
              className={inputCls}
              value={location}
              onChange={(e) => setLocation(e.target.value)}
              onKeyDown={acceptPlaceholderOnTab(setLocation)}
              placeholder={profileLocation ?? 'Toronto, ON'}
            />
          </label>
          <label className="block">
            <span className={labelCls}>Posted within</span>
            <select className={inputCls} value={datePosted} onChange={(e) => setDatePosted(e.target.value)}>
              <option value="">Any time</option>
              <option value="day">Past day</option>
              <option value="week">Past week</option>
              <option value="month">Past month</option>
            </select>
          </label>
          <div>
            <span className={labelCls}>Workplace</span>
            <div className="mt-1 flex gap-3">
              {WORKPLACE_OPTIONS.map((w) => (
                <label key={w} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={workplace.includes(w)}
                    onChange={(e) =>
                      setWorkplace((prev) => (e.target.checked ? [...prev, w] : prev.filter((x) => x !== w)))
                    }
                  />
                  {w}
                </label>
              ))}
            </div>
          </div>
          <label className="block">
            <span className={labelCls}>Max results</span>
            <input
              type="number"
              min={1}
              max={500}
              className={inputCls}
              value={cap}
              onChange={(e) => setCap(Number(e.target.value) || 100)}
            />
          </label>
          <div className="col-span-2 md:col-span-3">
            <span className={labelCls}>Experience level</span>
            <div className="mt-1 flex flex-wrap gap-3">
              {EXPERIENCE_OPTIONS.map((opt) => (
                <label key={opt.id} className="flex items-center gap-1 text-sm">
                  <input
                    type="checkbox"
                    checked={experience.includes(opt.id)}
                    onChange={(e) =>
                      setExperience((prev) =>
                        e.target.checked ? [...prev, opt.id] : prev.filter((x) => x !== opt.id),
                      )
                    }
                  />
                  {opt.label}
                </label>
              ))}
            </div>
          </div>
        </div>
        <div className="mt-4 flex items-center gap-3">
          <button
            className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-40"
            disabled={!keywords.trim() || run?.status === 'running'}
            onClick={start}
          >
            Start search
          </button>
          {run?.status === 'stopped_error' && (
            <button
              className="rounded-md border border-slate-300 px-4 py-2 text-sm hover:bg-slate-50"
              onClick={resume}
            >
              Resume
            </button>
          )}
          {run?.status && (
            <span className="text-sm text-slate-500">
              {RUN_LABELS[run.status] ?? run.status}
              {typeof run.collected === 'number' && ` · ${run.collected} collected`}
            </span>
          )}
        </div>
        {run?.message && <p className="mt-2 text-sm text-amber-700">{run.message}</p>}
        {error && <p className="mt-2 text-sm text-rose-600">{error}</p>}
      </section>

      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">
            Jobs ({visibleJobs.length}
            {visibleJobs.length !== jobs.length ? ` of ${jobs.length}` : ''})
          </h2>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {scoringPending > 0 && !scoringStall && (
              <span className="rounded-full bg-sky-100 px-3 py-1 text-xs font-medium text-sky-800">
                Scoring… {jobs.length - scoringPending}/{jobs.length}
              </span>
            )}
            {scoringPending > 0 && scoringStall && (
              <>
                <span
                  className="rounded-full bg-amber-100 px-3 py-1 text-xs font-medium text-amber-800"
                  title={scoringStall.detail ?? undefined}
                >
                  {scoringStall.reason === 'engine_blocked'
                    ? 'Scoring paused — Claude engine needs attention'
                    : 'Scoring paused — automatic retries exhausted'}
                </span>
                <button
                  className="rounded-md border border-amber-300 px-2 py-1 text-xs text-amber-800 hover:bg-amber-50"
                  onClick={async () => {
                    setError(null)
                    try {
                      await api.retryScoring()
                      await refresh()
                    } catch (e) {
                      setError(String(e))
                    }
                  }}
                >
                  Retry scoring
                </button>
              </>
            )}
            {selectedVisible.length > 0 && (
              <button
                className="rounded-full bg-slate-900 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700 disabled:opacity-40"
                disabled={busyGenerate || listStale}
                onClick={generatePackages}
                title="Tailored resume + cover letter PDFs for each selected job"
              >
                {busyGenerate
                  ? 'Queueing…'
                  : `Generate packages (${selectedVisible.length})`}
              </button>
            )}
            <label className="flex items-center gap-1">
              Sort
              <select
                className="rounded-md border border-slate-300 px-1 py-0.5 text-sm"
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as 'score' | 'date')}
              >
                <option value="score">Score</option>
                <option value="date">Newest</option>
              </select>
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={hideExceeds}
                onChange={(e) => setHideExceeds(e.target.checked)}
              />
              Hide over-experience
            </label>
            <label className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={easyOnly}
                onChange={(e) => setEasyOnly(e.target.checked)}
              />
              Easy Apply only
            </label>
            {scoredCount >= 2 && scoringPending === 0 && (
              <button
                className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-40"
                disabled={busyRerank}
                onClick={rerank}
                title="LLM-order the jobs scored within a few points of the top"
              >
                {busyRerank ? 'Reranking…' : 'Rerank close matches'}
              </button>
            )}
          </div>
        </div>
        {jobs.length === 0 ? (
          <p className="text-sm text-slate-400">No jobs collected yet.</p>
        ) : (
          <ul className="divide-y divide-slate-100">
            {visibleJobs.map((job) => {
              const score = displayScore(job)
              const breakdown = job.score_breakdown
              const explanation = explanations[job.job_id]
              return (
                <li key={job.job_id} className="py-3">
                  <div className="flex items-start justify-between gap-4">
                    <div className="flex items-start gap-3">
                      <input
                        type="checkbox"
                        className="mt-1.5"
                        checked={selected.has(job.job_id)}
                        disabled={listStale}
                        title={listStale ? 'New search results are loading…' : undefined}
                        onChange={() => toggleSelected(job.job_id)}
                      />
                      {score !== null && (
                        <span
                          className={`mt-0.5 w-12 shrink-0 rounded-md px-1.5 py-0.5 text-center text-sm font-semibold ${
                            score >= 75
                              ? 'bg-emerald-100 text-emerald-800'
                              : score >= 50
                                ? 'bg-amber-100 text-amber-800'
                                : 'bg-slate-100 text-slate-600'
                          }`}
                          title={
                            job.llm_rerank !== null
                              ? `LLM rerank ${job.llm_rerank} (deterministic ${job.deterministic_score})`
                              : `Deterministic score ${job.deterministic_score}`
                          }
                        >
                          {Math.round(score)}
                        </span>
                      )}
                      <div>
                        <a
                          href={job.url}
                          target="_blank"
                          rel="noreferrer"
                          className="font-medium text-slate-900 hover:underline"
                        >
                          {job.title}
                        </a>
                        <p className="text-sm text-slate-500">
                          {job.company}
                          {job.location ? ` · ${job.location}` : ''}
                          {job.posted_text ? ` · ${job.posted_text}` : ''}
                        </p>
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      {breakdown?.exceeds_experience && (
                        <span
                          className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-800"
                          title="Demands more experience than the profile shows"
                        >
                          Over-exp
                        </span>
                      )}
                      {job.easy_apply && (
                        <span className="rounded-full bg-sky-100 px-2 py-0.5 text-xs font-medium text-sky-800">
                          Easy Apply
                        </span>
                      )}
                      <button
                        className="rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-slate-50 disabled:opacity-40"
                        disabled={busyDetail === job.job_id}
                        onClick={() =>
                          job.description && expanded !== job.job_id
                            ? setExpanded(job.job_id)
                            : expanded === job.job_id
                              ? setExpanded(null)
                              : fetchDetail(job.job_id)
                        }
                      >
                        {busyDetail === job.job_id
                          ? 'Fetching…'
                          : expanded === job.job_id
                            ? 'Hide'
                            : 'Details'}
                      </button>
                    </div>
                  </div>
                  {expanded === job.job_id && (
                    <div className="mt-2 space-y-2">
                      {breakdown && (
                        <div className="rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
                          <p className="text-xs uppercase tracking-wide text-slate-400">
                            Skills {breakdown.skills} · Experience{' '}
                            {breakdown.experience} · Education {breakdown.education}
                          </p>
                          {breakdown.match_reasons.map((r) => (
                            <p key={r} className="mt-1 text-emerald-700">
                              ✓ {r}
                            </p>
                          ))}
                          {breakdown.gap_reasons.map((r) => (
                            <p key={r} className="mt-1 text-amber-700">
                              ✗ {r}
                            </p>
                          ))}
                          {job.deterministic_score !== null && (
                            <button
                              className="mt-2 rounded-md border border-slate-300 px-2 py-1 text-xs hover:bg-white disabled:opacity-40"
                              disabled={busyExplain === job.job_id}
                              onClick={() => explain(job.job_id)}
                            >
                              {busyExplain === job.job_id
                                ? 'Explaining…'
                                : explanation
                                  ? 'Refresh explanation'
                                  : 'Explain fit'}
                            </button>
                          )}
                          {explanation && (
                            <div className="mt-2 border-t border-slate-200 pt-2">
                              <p className="font-medium text-slate-800">
                                {explanation.pitch}
                              </p>
                              {explanation.strengths.map((s) => (
                                <p key={s} className="mt-1 text-emerald-700">
                                  + {s}
                                </p>
                              ))}
                              {explanation.gaps.map((g) => (
                                <p key={g} className="mt-1 text-amber-700">
                                  – {g}
                                </p>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                      {job.description && (
                        <p className="whitespace-pre-wrap rounded-lg bg-slate-50 p-3 text-sm text-slate-700">
                          {job.description}
                        </p>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>

      {packages.length > 0 && (
        <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <h2 className="mb-4 text-lg font-semibold">Packages</h2>
          <ul className="divide-y divide-slate-100">
            {packages.map((pkg) => {
              const state = PACKAGE_STATES[pkg.status] ?? {
                label: pkg.status,
                tone: 'bg-slate-100 text-slate-600',
              }
              return (
                <li
                  key={pkg.id}
                  className="flex flex-wrap items-center justify-between gap-3 py-3"
                >
                  <div className="min-w-0">
                    <p className="truncate font-medium text-slate-800">
                      {pkg.title}{' '}
                      <span className="font-normal text-slate-500">
                        — {pkg.company}
                      </span>
                    </p>
                    {pkg.error && pkg.status !== 'done' && (
                      <p className="mt-0.5 text-xs text-rose-600">{pkg.error}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-3 text-sm">
                    <span
                      className={`rounded-full px-3 py-1 text-xs font-medium ${state.tone}`}
                    >
                      {state.label}
                    </span>
                    {pkg.status === 'done' && (
                      <>
                        <a
                          className="text-slate-600 underline hover:text-slate-900"
                          href={packageFileUrl(pkg.id, 'resume.pdf')}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Resume
                        </a>
                        <a
                          className="text-slate-600 underline hover:text-slate-900"
                          href={packageFileUrl(pkg.id, 'cover_letter.pdf')}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Cover letter
                        </a>
                        <a
                          className="text-slate-600 underline hover:text-slate-900"
                          href={packageFileUrl(pkg.id, 'job.md')}
                          target="_blank"
                          rel="noreferrer"
                        >
                          Posting
                        </a>
                      </>
                    )}
                    <a
                      className="text-slate-400 underline hover:text-slate-600"
                      href={pkg.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      LinkedIn
                    </a>
                  </div>
                </li>
              )
            })}
          </ul>
        </section>
      )}
    </div>
  )
}
