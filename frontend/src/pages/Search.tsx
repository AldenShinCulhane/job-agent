import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { api, packageFileUrl } from '../api'
import type { Explanation, Job, LinkedInStatus, Package, SearchRun } from '../api'
import { useEpochEffect } from '../useEventStream'
import type { Epochs } from '../useEventStream'
import { useLatestOnly } from '../useLatestOnly'
import { badgeFor, btnPrimary, btnSecondary, chip, inputCls, labelCls } from '../styles'
import type { Badge } from '../styles'
import type { ReadOptions } from '../useLatestOnly'

const LINKEDIN_STATES: Record<string, Badge> = {
  idle: { label: 'Not connected', tone: 'neutral' },
  connecting: { label: 'Connecting…', tone: 'info' },
  ready: { label: 'LinkedIn connected', tone: 'ok' },
  logged_out: { label: 'Sign-in needed', tone: 'warn' },
  captcha_stop: { label: 'Checkpoint — stopped', tone: 'danger' },
  error: { label: 'Browser error', tone: 'danger' },
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

const PACKAGE_STATES: Record<string, Badge> = {
  queued: { label: 'Queued', tone: 'neutral' },
  generating: { label: 'Generating…', tone: 'info' },
  validating: { label: 'Validating…', tone: 'info' },
  fitting: { label: 'Fitting pages…', tone: 'info' },
  asserting: { label: 'Final checks…', tone: 'info' },
  publishing: { label: 'Publishing…', tone: 'info' },
  done: { label: 'Ready', tone: 'ok' },
  failed: { label: 'Failed', tone: 'danger' },
  waiting_usage: { label: 'Waiting — usage limit', tone: 'warn' },
}

const PACKAGE_ACTIVE = new Set([
  'queued', 'generating', 'validating', 'fitting', 'asserting', 'publishing',
])

const WORKPLACE_OPTIONS = ['remote', 'hybrid', 'onsite'] as const

const DATE_LABELS: Record<string, string> = {
  day: 'Past day',
  week: 'Past week',
  month: 'Past month',
}

// How many of the newest packages stay on screen outside the fold. The
// list is every package ever generated, so it is bounded here; anything
// still in flight is shown regardless of age.
const RECENT_PACKAGES = 5

const CONSENT =
  'A real browser window opens for sign-in; your session stays on this machine. ' +
  'Sign in with your LinkedIn email and password — Google blocks its sign-in ' +
  'inside automated browsers, so "Continue with Google" won\'t work here. ' +
  'Automated collection is paced but still against LinkedIn\'s terms — account ' +
  'restriction is a risk you accept (see README).'

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
  // Whether the search fields are on screen while a run exists to
  // summarise instead. Only the user's own actions change it — typing
  // and Edit open the fields, Cancel and a successful Start close them —
  // so no response arriving later can fold an editor that is in use.
  // With no run there is nothing to summarise and the fields simply show.
  const [editing, setEditing] = useState(false)
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
      setEditing(false)
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

  // What is BOTH selected and currently on screen — the filtered list,
  // not the full one. The list can be replaced under an existing
  // selection, and a job id that is no longer rendered must never be
  // submitted, whichever response won the race; a filter hides a row the
  // same way, and a hidden row is not something the user can see they
  // are about to generate for.
  const selectedVisible = useMemo(
    () => visibleJobs.filter((j) => selected.has(j.job_id)).map((j) => j.job_id),
    [visibleJobs, selected],
  )

  const scoredCount = jobs.filter((j) => j.deterministic_score !== null).length

  const badge = linkedin ? badgeFor(LINKEDIN_STATES, linkedin.state) : null

  const fieldsOpen = run === null || editing

  // The fields start from the run they would replace, so an edit is a
  // change to the last search rather than a blank form. Local state from
  // here on: nothing writes these again until the next Edit.
  const beginEdit = () => {
    const c = run?.config
    if (c) {
      setKeywords(c.keywords)
      setLocation(c.location ?? '')
      setDatePosted(c.date_posted ?? '')
      setWorkplace(c.workplace ?? [])
      setExperience(c.experience ?? [])
      setCap(c.cap ?? 100)
    }
    setEditing(true)
  }
  // Typing is the user opening the editor, even when it was merely
  // showing because no run existed yet: a run started elsewhere must
  // not fold the fields under them.
  const touch = () => setEditing(true)

  // What the persisted run searched for — the summary describes the run,
  // not whatever the fields currently hold.
  const summary: string[] = run?.config
    ? [
        run.config.keywords,
        run.config.location ?? null,
        run.config.date_posted
          ? (DATE_LABELS[run.config.date_posted] ?? run.config.date_posted)
          : null,
        run.config.workplace?.length ? run.config.workplace.join(' / ') : null,
        run.config.experience?.length
          ? run.config.experience
              .map((id) => EXPERIENCE_OPTIONS.find((o) => o.id === id)?.label ?? id)
              .join(', ')
          : null,
        run.config.cap ? `up to ${run.config.cap}` : null,
      ].filter((x): x is string => x !== null)
    : []

  // Newest first from the server. Everything still moving stays visible
  // however far down it is; the rest beyond the first few folds away.
  const inFlight = (p: Package) =>
    PACKAGE_ACTIVE.has(p.status) || p.status === 'waiting_usage'
  const recentPackages = packages.filter((p, i) => i < RECENT_PACKAGES || inFlight(p))
  const earlierPackages = packages.filter((p, i) => i >= RECENT_PACKAGES && !inFlight(p))

  const packageRow = (pkg: Package) => {
    const state = badgeFor(PACKAGE_STATES, pkg.status)
    return (
      <li key={pkg.id} className="py-3">
        <p className="font-medium text-ink">
          {pkg.title}{' '}
          <span className="font-normal text-ink-3">— {pkg.company}</span>
        </p>
        {pkg.error && pkg.status !== 'done' && (
          <p className="mt-0.5 text-xs text-danger-fg">{pkg.error}</p>
        )}
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className={`rounded-full px-3 py-1 text-xs font-medium ${chip[state.tone]}`}>
            {state.label}
          </span>
          {pkg.status === 'done' && (
            <>
              <a
                className="text-ink-2 underline hover:text-ink"
                href={packageFileUrl(pkg.id, 'resume.pdf')}
                target="_blank"
                rel="noreferrer"
              >
                Resume
              </a>
              <a
                className="text-ink-2 underline hover:text-ink"
                href={packageFileUrl(pkg.id, 'cover_letter.pdf')}
                target="_blank"
                rel="noreferrer"
              >
                Cover letter
              </a>
              <a
                className="text-ink-2 underline hover:text-ink"
                href={packageFileUrl(pkg.id, 'job.md')}
                target="_blank"
                rel="noreferrer"
              >
                Posting
              </a>
            </>
          )}
          <a
            className="text-ink-3 underline hover:text-ink-2"
            href={pkg.url}
            target="_blank"
            rel="noreferrer"
          >
            LinkedIn
          </a>
        </div>
      </li>
    )
  }

  const card = 'rounded-xl border border-line bg-surface p-4 shadow-card sm:p-6'

  return (
    // One column, in reading order: connection, search, what is being
    // generated, then the long list. From xl the packages become a rail
    // beside the list — same DOM order, so the reading order holds.
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_20rem] xl:items-start">
      <section className={`${card} xl:col-start-1 xl:row-start-1`}>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">LinkedIn</h2>
          <div className="flex flex-wrap items-center gap-3">
            {badge && (
              <span className={`rounded-full px-3 py-1 text-sm font-medium ${chip[badge.tone]}`}>
                {badge.label}
              </span>
            )}
            {linkedin?.state !== 'ready' && (
              <button
                className={`${btnPrimary} rounded-md px-3 py-1.5 text-sm font-medium`}
                disabled={linkedin?.state === 'connecting'}
                onClick={connect}
              >
                Connect
              </button>
            )}
          </div>
        </div>
        {linkedin?.detail && <p className="mt-2 text-sm text-warn-fg">{linkedin.detail}</p>}
        {linkedin?.state === 'ready' ? (
          // Connected: the disclosure has been acted on, so it folds. Any
          // problem with the connection stays on the line above.
          <details className="mt-2 text-xs text-ink-3">
            <summary className="cursor-pointer">Browser access and account risk</summary>
            <p className="mt-1">{CONSENT}</p>
          </details>
        ) : (
          <p className="mt-2 text-xs text-ink-3">{CONSENT}</p>
        )}
      </section>

      <section className={`${card} xl:col-start-1 xl:row-start-2`}>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Job search</h2>
          {run !== null && !fieldsOpen && (
            <button
              className={`${btnSecondary} rounded-md px-3 py-1.5 text-sm`}
              aria-expanded={false}
              onClick={beginEdit}
            >
              Edit search
            </button>
          )}
        </div>
        {fieldsOpen ? (
          <>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <label className="col-span-2 block">
                <span className={labelCls}>Keywords</span>
                <input
                  className={inputCls}
                  value={keywords}
                  onChange={(e) => {
                    touch()
                    setKeywords(e.target.value)
                  }}
                  onKeyDown={acceptPlaceholderOnTab(setKeywords)}
                  placeholder="software engineer"
                />
              </label>
              <label className="block">
                <span className={labelCls}>Location</span>
                <input
                  className={inputCls}
                  value={location}
                  onChange={(e) => {
                    touch()
                    setLocation(e.target.value)
                  }}
                  onKeyDown={acceptPlaceholderOnTab(setLocation)}
                  placeholder={profileLocation ?? 'Toronto, ON'}
                />
              </label>
              <label className="block">
                <span className={labelCls}>Posted within</span>
                <select
                  className={inputCls}
                  value={datePosted}
                  onChange={(e) => {
                    touch()
                    setDatePosted(e.target.value)
                  }}
                >
                  <option value="">Any time</option>
                  <option value="day">Past day</option>
                  <option value="week">Past week</option>
                  <option value="month">Past month</option>
                </select>
              </label>
              <div>
                <span className={labelCls}>Workplace</span>
                <div className="mt-1 flex flex-wrap gap-3">
                  {WORKPLACE_OPTIONS.map((w) => (
                    <label key={w} className="flex items-center gap-1 text-sm">
                      <input
                        type="checkbox"
                        checked={workplace.includes(w)}
                        onChange={(e) => {
                          touch()
                          setWorkplace((prev) =>
                            e.target.checked ? [...prev, w] : prev.filter((x) => x !== w),
                          )
                        }}
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
                  onChange={(e) => {
                    touch()
                    setCap(Number(e.target.value) || 100)
                  }}
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
                        onChange={(e) => {
                          touch()
                          setExperience((prev) =>
                            e.target.checked
                              ? [...prev, opt.id]
                              : prev.filter((x) => x !== opt.id),
                          )
                        }}
                      />
                      {opt.label}
                    </label>
                  ))}
                </div>
              </div>
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <button
                className={`${btnPrimary} rounded-md px-4 py-2 text-sm font-medium`}
                disabled={!keywords.trim() || run?.status === 'running'}
                onClick={start}
              >
                Start search
              </button>
              {run !== null && (
                <button
                  className={`${btnSecondary} rounded-md px-4 py-2 text-sm`}
                  onClick={() => setEditing(false)}
                >
                  Cancel
                </button>
              )}
            </div>
          </>
        ) : (
          <ul className="flex flex-wrap gap-2" aria-label="Last search">
            {summary.map((item) => (
              <li
                key={item}
                className={`rounded-full px-3 py-1 text-sm ${chip.neutral}`}
              >
                {item}
              </li>
            ))}
          </ul>
        )}
        {/* The run's own state, and anything that failed on this page,
            stay in view whether the fields are open or folded. */}
        {run?.status && (
          <div className="mt-3 flex flex-wrap items-center gap-3">
            {run.status === 'stopped_error' && (
              <button
                className={`${btnSecondary} rounded-md px-4 py-2 text-sm`}
                onClick={resume}
              >
                Resume
              </button>
            )}
            <span className="text-sm text-ink-3">
              {RUN_LABELS[run.status] ?? run.status}
              {typeof run.collected === 'number' && ` · ${run.collected} collected`}
            </span>
          </div>
        )}
        {run?.message && <p className="mt-2 text-sm text-warn-fg">{run.message}</p>}
        {error && <p className="mt-2 text-sm text-danger-fg">{error}</p>}
      </section>

      <section
        className={`${card} xl:sticky xl:top-4 xl:col-start-2 xl:row-span-3 xl:row-start-1 xl:self-start`}
      >
        <h2 className="mb-2 text-lg font-semibold">Packages</h2>
        {packages.length === 0 ? (
          <p className="text-sm text-ink-3">
            None yet. Tick jobs in the list and generate packages for them.
          </p>
        ) : (
          // From xl the rail is as tall as the viewport allows and scrolls
          // on its own; the tab stop is what makes that reachable without
          // a pointer.
          <div
            className="xl:max-h-[calc(100vh-7rem)] xl:overflow-y-auto"
            tabIndex={0}
            role="region"
            aria-label="Package list"
          >
            <ul className="divide-y divide-line">{recentPackages.map(packageRow)}</ul>
            {earlierPackages.length > 0 && (
              <details className="mt-2 border-t border-line pt-2">
                <summary className="cursor-pointer text-sm text-ink-2">
                  Earlier packages ({earlierPackages.length})
                </summary>
                <ul className="divide-y divide-line">{earlierPackages.map(packageRow)}</ul>
              </details>
            )}
          </div>
        )}
      </section>

      <section className={`${card} xl:col-start-1 xl:row-start-3`}>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="text-lg font-semibold">
              Jobs ({visibleJobs.length}
              {visibleJobs.length !== jobs.length ? ` of ${jobs.length}` : ''})
            </h2>
            {scoringPending > 0 && !scoringStall && (
              <span className={`rounded-full px-3 py-1 text-xs font-medium ${chip.info}`}>
                Scoring… {jobs.length - scoringPending}/{jobs.length}
              </span>
            )}
            {scoringPending > 0 && scoringStall && (
              <>
                <span
                  className={`rounded-full px-3 py-1 text-xs font-medium ${chip.warn}`}
                  title={scoringStall.detail ?? undefined}
                >
                  {scoringStall.reason === 'engine_blocked'
                    ? 'Scoring paused — Claude engine needs attention'
                    : 'Scoring paused — automatic retries exhausted'}
                </span>
                <button
                  className="rounded-md border border-line-strong px-2 py-1 text-xs text-warn-fg hover:bg-warn-bg"
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
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {selectedVisible.length > 0 && (
              <button
                className={`${btnPrimary} rounded-full px-3 py-1 text-xs font-medium`}
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
                className="rounded-md border border-line-strong bg-surface px-1 py-0.5 text-sm text-ink"
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
                className={`${btnSecondary} rounded-md px-2 py-1 text-xs`}
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
          <p className="text-sm text-ink-3">No jobs collected yet.</p>
        ) : (
          <ul className="divide-y divide-line">
            {visibleJobs.map((job) => {
              const score = displayScore(job)
              const breakdown = job.score_breakdown
              const explanation = explanations[job.job_id]
              const meta = [job.company, job.location, job.posted_text]
                .filter(Boolean)
                .join(' · ')
              return (
                // Four columns from md: tick, score, title, actions — so
                // the eye runs down aligned scores and titles. Below md
                // the actions drop under the title instead of squeezing it.
                <li
                  key={job.job_id}
                  className="grid grid-cols-[auto_auto_minmax(0,1fr)] items-start gap-x-3 gap-y-1 py-1.5 md:grid-cols-[auto_auto_minmax(0,1fr)_auto]"
                >
                  <input
                    type="checkbox"
                    className="mt-1"
                    checked={selected.has(job.job_id)}
                    disabled={listStale}
                    title={listStale ? 'New search results are loading…' : undefined}
                    aria-label={`Select ${job.title}`}
                    onChange={() => toggleSelected(job.job_id)}
                  />
                  {score !== null ? (
                    <span
                      className={`w-12 rounded-md px-1.5 py-0.5 text-center text-sm font-semibold ${
                        chip[score >= 75 ? 'ok' : score >= 50 ? 'warn' : 'neutral']
                      }`}
                      title={
                        job.llm_rerank !== null
                          ? `LLM rerank ${job.llm_rerank} (deterministic ${job.deterministic_score})`
                          : `Deterministic score ${job.deterministic_score}`
                      }
                    >
                      {Math.round(score)}
                    </span>
                  ) : (
                    <span className="w-12" aria-hidden="true" />
                  )}
                  <div className="min-w-0">
                    <a
                      href={job.url}
                      target="_blank"
                      rel="noreferrer"
                      className="font-medium leading-tight text-ink hover:underline"
                    >
                      {job.title}
                    </a>
                    <p className="truncate text-sm text-ink-3" title={meta}>
                      {meta}
                    </p>
                  </div>
                  <div className="col-start-3 flex flex-wrap items-center gap-2 md:col-start-4 md:row-start-1">
                    {breakdown?.exceeds_experience && (
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-medium ${chip.warn}`}
                        title="Demands more experience than the profile shows"
                      >
                        Over-exp
                      </span>
                    )}
                    {job.easy_apply && (
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${chip.info}`}>
                        Easy Apply
                      </span>
                    )}
                    <button
                      className={`${btnSecondary} rounded-md px-2 py-1 text-xs`}
                      disabled={busyDetail === job.job_id}
                      aria-expanded={expanded === job.job_id}
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
                  {expanded === job.job_id && (
                    <div className="col-span-full mt-1 space-y-2">
                      {breakdown && (
                        <div className="rounded-lg bg-surface-2 p-3 text-sm text-ink-2">
                          <p className="text-xs uppercase tracking-wide text-ink-3">
                            Skills {breakdown.skills} · Experience{' '}
                            {breakdown.experience} · Education {breakdown.education}
                          </p>
                          {breakdown.match_reasons.map((r) => (
                            <p key={r} className="mt-1 text-ok-fg">
                              ✓ {r}
                            </p>
                          ))}
                          {breakdown.gap_reasons.map((r) => (
                            <p key={r} className="mt-1 text-warn-fg">
                              ✗ {r}
                            </p>
                          ))}
                          {job.deterministic_score !== null && (
                            <button
                              className="mt-2 rounded-md border border-line-strong px-2 py-1 text-xs hover:bg-surface disabled:opacity-40"
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
                            <div className="mt-2 border-t border-line pt-2">
                              <p className="font-medium text-ink">
                                {explanation.pitch}
                              </p>
                              {explanation.strengths.map((s) => (
                                <p key={s} className="mt-1 text-ok-fg">
                                  + {s}
                                </p>
                              ))}
                              {explanation.gaps.map((g) => (
                                <p key={g} className="mt-1 text-warn-fg">
                                  – {g}
                                </p>
                              ))}
                            </div>
                          )}
                        </div>
                      )}
                      {job.description && (
                        <p className="whitespace-pre-wrap rounded-lg bg-surface-2 p-3 text-sm text-ink-2">
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
    </div>
  )
}
