import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { api, packageFileUrl, RetryUnconfirmed } from '../api'
import type { Explanation, Job, LinkedInStatus, Package, Requirement, SearchRun } from '../api'
import { paragraphs } from '../description'
import { useEpochEffect } from '../useEventStream'
import type { Epochs } from '../useEventStream'
import { useLatestOnly } from '../useLatestOnly'
import { badgeFor, btnPrimary, btnSecondary, chip, inputCls, labelCls } from '../styles'
import { useFocusLanding, useHeldFocus } from '../focus'
import { listedOn, roleRows, selectableIds } from '../jobGroups'
import type { Listing } from '../jobGroups'
import {
  attemptLabel,
  failedFirst,
  newestByJob,
  newestPerJob,
  requestKey,
  retryAction,
  truthCheckSummary,
} from '../packageAttempts'
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

// What a job row says about its newest package: the state in a word,
// the stages folded into one — the rail beside the list has the detail.
const JOB_PACKAGE_STATES: Record<string, Badge> = {
  ...Object.fromEntries(
    [...PACKAGE_ACTIVE].map((s) => [s, { label: 'Package in progress', tone: 'info' }]),
  ),
  done: { label: 'Package ready', tone: 'ok' },
  failed: { label: 'Package failed', tone: 'danger' },
  waiting_usage: { label: 'Package waiting', tone: 'warn' },
}

// The checklist's groups, in the order a posting usually states them.
const REQUIREMENT_GROUPS: [string, (r: Requirement) => boolean][] = [
  ['Required', (r) => r.kind === 'required'],
  ['Nice to have', (r) => r.kind === 'preferred'],
  ['Experience and education', (r) => r.kind === 'experience' || r.kind === 'education'],
]

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

// What the two focus slots below hold: a retry button, and a control in
// a row of the job list, which says what posting it acts for.
const isRetryButton = (el: HTMLElement) => 'retry' in el.dataset
const inJobRow = (el: HTMLElement) => el.closest('[data-posting]') !== null

export default function Search({
  epochs,
  connected,
  onHold,
}: {
  epochs: Epochs
  connected: boolean
  /** Told whether this page has work the user would lose if it went away. */
  onHold: (hold: boolean) => void
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
  // Roles whose other listings are shown, by every posting of the role:
  // its row can be rebuilt to stand for another of them, and what was
  // shown stays shown.
  const [openListings, setOpenListings] = useState<Set<string>>(new Set())
  const [packages, setPackages] = useState<Package[]>([])
  const [busyGenerate, setBusyGenerate] = useState(false)
  // Try again / Regenerate clicks still out, and what each last said
  // when it failed, by the package it was pressed on.
  const [retrying, setRetrying] = useState<Set<number>>(new Set())
  const [retryErrors, setRetryErrors] = useState<Record<number, string>>({})
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
  // Typed search fields, or an answer of theirs still on its way.
  const held =
    editing || busyGenerate || busyRerank || busyExplain !== null || busyDetail !== null ||
    retrying.size > 0
  useEffect(() => onHold(held), [held, onHold])
  useEffect(() => () => onHold(false), [onHold])
  // Bumped by every edit. Start captures it and folds the fields on
  // completion only if nothing was typed while the request was out;
  // otherwise the newer text is exactly what must stay on screen.
  const editSeq = useRef(0)
  // What the gate itself said when it last failed, shown beside it.
  const [generateError, setGenerateError] = useState<string | null>(null)
  // The selection bar, while it exists. Sticky to the viewport bottom,
  // it covers whatever the page scrolls under it — including a row the
  // keyboard just focused. scroll-padding on the root is how the browser
  // is told that the bottom of the viewport ends where the bar starts.
  const barRef = useRef<HTMLDivElement | null>(null)
  // Landing spots for focus when the control that had it goes away.
  const editButtonRef = useRef<HTMLButtonElement | null>(null)
  const keywordsRef = useRef<HTMLInputElement | null>(null)
  const jobsHeadingRef = useRef<HTMLHeadingElement | null>(null)
  const jobListRef = useRef<HTMLUListElement | null>(null)
  const packagesHeadingRef = useRef<HTMLHeadingElement | null>(null)
  const land = useFocusLanding()
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
  // A saved description can join or split postings that repeat a role.
  useEpochEffect(epochs.jobs, refreshJobs)
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
    const seq = editSeq.current
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
      if (editSeq.current === seq) {
        setEditing(false)
        land(editButtonRef)
      }
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
    setGenerateError(null)
    try {
      // Sent with the run the rows came from — the server rejects ids
      // that are not in it rather than generating for a job the user
      // never saw.
      await api.packagesGenerate(selectedVisible, jobsRunId)
      setSelected(new Set())
      land(packagesHeadingRef)
      await refresh()
    } catch (e) {
      // Beside the button that failed, not in the search card a screen
      // above it.
      setGenerateError(String(e))
    } finally {
      setBusyGenerate(false)
    }
  }

  // The retry button that holds the focus, if one does. The list can
  // drop it at any time — a click whose answer was lost did land, a
  // refusal says another tab got there first, another tab's click shows
  // up — and the focus would fall to the top of the page; it goes to the
  // Packages heading instead. One slot is right however many clicks are
  // out, because only one element holds the focus. A refusal that leaves
  // the button leaves the focus on it.
  const focusedRetry = useHeldFocus(isRetryButton)
  // Still held but gone from the page: the focus went with it.
  useEffect(() => {
    const el = focusedRetry.current
    if (el === null || el.isConnected) return
    focusedRetry.current = null
    land(packagesHeadingRef)
  }, [packages, land, focusedRetry])

  // Try again on a failed package, Regenerate on a done one: a fresh
  // package row, only ever on this click. The key names the click, so the
  // server answers a resend of it with the package it already got. The
  // ref, not the state, is the guard: a second click can land before the
  // button re-renders, and the button stays focusable while the click is
  // out.
  const retryInFlight = useRef<Set<number>>(new Set())
  const retry = async (pkg: Package) => {
    if (retryInFlight.current.has(pkg.id)) return
    retryInFlight.current.add(pkg.id)
    const key = requestKey()
    setRetrying((prev) => new Set(prev).add(pkg.id))
    setRetryErrors((prev) => {
      const next = { ...prev }
      delete next[pkg.id]
      return next
    })
    try {
      await api.packageRetry(pkg.id, key)
      // The new row becomes the job's newest and this button goes with
      // it; focus goes to the list rather than to the top of the page —
      // also from a click that never focused the button, as in Safari.
      land(packagesHeadingRef)
      await refreshPackages()
    } catch (e) {
      const message = e instanceof RetryUnconfirmed ? e.message : String(e)
      setRetryErrors((prev) => ({ ...prev, [pkg.id]: message }))
      // A lost answer may still have made the row, or a refusal may say
      // the list is behind: either way, read it again. Another click
      // cannot double it — this row is no longer the job's newest.
      void refreshPackages()
    } finally {
      retryInFlight.current.delete(pkg.id)
      setRetrying((prev) => {
        const next = new Set(prev)
        next.delete(pkg.id)
        return next
      })
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

  // Each job's newest package: its row's badge, and which posting a
  // role's row stands for.
  const jobPackage = useMemo(() => newestByJob(packages), [packages])

  // Filters judge postings; the list shows roles. Postings that repeat
  // one role fold into one row, which stands for one of them.
  const rows = useMemo(
    () => roleRows(visibleJobs, (id) => jobPackage.has(id)),
    [visibleJobs, jobPackage],
  )
  const roleCount = useMemo(
    () => new Set(jobs.map((j) => j.group?.id ?? `posting:${j.job_id}`)).size,
    [jobs],
  )

  // What is BOTH selected and currently on screen — a row, or a listing
  // a row shows — not the full list. The list can be replaced under an
  // existing selection, and a job id that is no longer rendered must
  // never be submitted, whichever response won the race; a filter hides
  // a posting the same way, and a hidden posting is not something the
  // user can see they are about to generate for.
  const selectedVisible = useMemo(
    () => selectableIds(rows).filter((id) => selected.has(id)),
    [rows, selected],
  )

  // The job-list control that holds the focus, if one does. A row stands
  // for one posting of its role, and which one can change under the
  // focus — a refresh folds the posting into another row's listings, a
  // listing gets a package and becomes the row — so the row is rebuilt,
  // and the focus would fall to the top of the page. It follows the
  // posting instead: to the same control wherever the posting is now,
  // shown if it is folded away; else to the posting's checkbox or link;
  // else, the posting gone from the list, to the Jobs heading. After
  // every commit, since any update can take the control away.
  const focusedJobControl = useHeldFocus(inJobRow)
  useEffect(() => {
    const el = focusedJobControl.current
    if (el === null || el.isConnected) return
    const posting = el.closest<HTMLElement>('[data-posting]')?.dataset.posting?.split(' ')[0]
    const at = (control: string | undefined) => control && posting
      ? jobListRef.current?.querySelector<HTMLElement>(
        `[data-control="${control}"][data-posting~="${CSS.escape(posting)}"]`)
      : null
    const target = at(el.dataset.control) ?? at('select') ?? at('link')
    if (!target) {
      focusedJobControl.current = null
      land(jobsHeadingRef)
      return
    }
    // Shown, if folded away; a toggle is never folded away.
    const fold = target.closest('details')
    if (fold && !fold.open && !target.matches('summary')) fold.open = true
    target.focus()
  })
  const showListings = (role: string[], open: boolean) =>
    setOpenListings((prev) => {
      if (role.every((id) => prev.has(id) === open)) return prev
      const next = new Set(prev)
      for (const id of role) {
        if (open) next.add(id)
        else next.delete(id)
      }
      return next
    })

  const hasSelection = selectedVisible.length > 0
  useEffect(() => {
    const bar = barRef.current
    if (!hasSelection || !bar) return
    const root = document.documentElement
    const apply = () => {
      root.style.scrollPaddingBottom = `${bar.getBoundingClientRect().height}px`
    }
    apply()
    const observer = new ResizeObserver(apply)
    observer.observe(bar)
    return () => {
      observer.disconnect()
      root.style.removeProperty('scroll-padding-bottom')
    }
  }, [hasSelection])

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
    land(keywordsRef)
  }
  // Typing is the user opening the editor, even when it was merely
  // showing because no run existed yet: a run started elsewhere must
  // not fold the fields under them.
  const touch = () => {
    editSeq.current += 1
    setEditing(true)
  }
  const edited = (setter: (value: string) => void) => (value: string) => {
    touch()
    setter(value)
  }

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

  // Newest first from the server, with a failure still waiting on the
  // user moved to the top. The preview is a fixed few whatever their
  // state — a bulk generation would otherwise put every queued package on
  // screen above the list — and the counts line says what the fold is
  // hiding.
  const newest = newestPerJob(packages)
  const shown = failedFirst(packages, newest)
  const recentPackages = shown.slice(0, RECENT_PACKAGES)
  const earlierPackages = shown.slice(RECENT_PACKAGES)
  const counts = (
    [
      [packages.filter((p) => PACKAGE_ACTIVE.has(p.status)).length, 'in progress'],
      [packages.filter((p) => p.status === 'waiting_usage').length, 'waiting'],
      [packages.filter((p) => p.status === 'failed').length, 'failed'],
      [packages.filter((p) => p.status === 'done').length, 'ready'],
    ] as [number, string][]
  )
    .filter(([n]) => n > 0)
    .map(([n, label]) => `${n} ${label}`)
    .join(' · ')

  const packageRow = (pkg: Package) => {
    const state = badgeFor(PACKAGE_STATES, pkg.status)
    const label = attemptLabel(pkg)
    const action = retryAction(pkg, newest)
    return (
      <li key={pkg.id} className="py-3">
        <p className="font-medium text-ink">
          {pkg.title}{' '}
          <span className="font-normal text-ink-3">— {pkg.company}</span>
        </p>
        {label && <p className="mt-0.5 text-xs text-ink-2">{label}</p>}
        {pkg.unsupported && pkg.unsupported.length > 0 ? (
          // A truth-check failure, in the words that failed: the claim,
          // where it was, and why. The claim folds to three lines until
          // the reason is opened.
          <div className="mt-1 space-y-1.5 text-xs">
            <p className="text-danger-fg">
              Stopped at the truth check: {truthCheckSummary(pkg.unsupported)}
            </p>
            {pkg.unsupported.map((u, i) => (
              <figure key={i} className="group">
                <figcaption className="text-ink-3">
                  {u.document === 'cover letter' ? 'Cover letter' : 'Resume'}
                </figcaption>
                {u.claim && (
                  <blockquote className="mt-0.5 line-clamp-3 border-l-2 border-line-strong pl-2 text-ink-2 group-has-[details[open]]:line-clamp-none">
                    “{u.claim}”
                  </blockquote>
                )}
                <details className="mt-0.5">
                  <summary className="cursor-pointer text-ink-3">Why</summary>
                  <p className="mt-0.5 text-ink-2">{u.reason}</p>
                </details>
              </figure>
            ))}
          </div>
        ) : (
          pkg.error &&
          pkg.status !== 'done' && <p className="mt-0.5 text-xs text-danger-fg">{pkg.error}</p>
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
          {action && (
            // aria-disabled, not disabled, while the click is out: a
            // disabled button drops the focus it has, and a refused retry
            // would leave the keyboard at the top of the page. The ref in
            // retry() is what stops a second request.
            <button
              className={`rounded-md px-2 py-0.5 text-xs ${btnSecondary} aria-disabled:opacity-40`}
              aria-disabled={retrying.has(pkg.id)}
              data-retry=""
              // Every row has one; the title says which package it is for.
              aria-label={`${retrying.has(pkg.id) ? 'Starting…' : action} ${pkg.title}`}
              onClick={() => void retry(pkg)}
            >
              {retrying.has(pkg.id) ? 'Starting…' : action}
            </button>
          )}
        </div>
        {retryErrors[pkg.id] && (
          <p className="mt-1 text-xs text-danger-fg">{retryErrors[pkg.id]}</p>
        )}
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
              ref={editButtonRef}
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
                  ref={keywordsRef}
                  className={inputCls}
                  value={keywords}
                  onChange={(e) => {
                    touch()
                    setKeywords(e.target.value)
                  }}
                  onKeyDown={acceptPlaceholderOnTab(edited(setKeywords))}
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
                  onKeyDown={acceptPlaceholderOnTab(edited(setLocation))}
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
                  onClick={() => {
                    setEditing(false)
                    land(editButtonRef)
                  }}
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
        <h2 ref={packagesHeadingRef} tabIndex={-1} className="mb-2 text-lg font-semibold">
          Packages
        </h2>
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
            {counts && <p className="text-xs text-ink-3">{counts}</p>}
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
            <h2 ref={jobsHeadingRef} tabIndex={-1} className="text-lg font-semibold">
              Jobs ({rows.length}
              {rows.length !== roleCount ? ` of ${roleCount}` : ''})
            </h2>
            {roleCount !== jobs.length && (
              <span className="text-sm text-ink-3">{jobs.length} listings</span>
            )}
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
                    ? 'Scoring paused — the AI engine needs attention'
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
          <ul ref={jobListRef} className="divide-y divide-line">
            {rows.map(({ job, listings }) => {
              const score = displayScore(job)
              const breakdown = job.score_breakdown
              const explanation = explanations[job.job_id]
              const meta = [job.company, job.location, job.posted_text]
                .filter(Boolean)
                .join(' · ')
              // The role's newest package: this posting's own, or else
              // another listing's — shown here, and named by its listing.
              const ownPackage = jobPackage.get(job.job_id)
              const listed = listings
                .map((listing) => ({ listing, pkg: jobPackage.get(listing.member.job_id) }))
                .filter((x): x is { listing: Listing; pkg: Package } => x.pkg !== undefined)
                .sort((a, b) => b.pkg.id - a.pkg.id)[0]
              const newestPackage = ownPackage ?? listed?.pkg
              const packageBadge = newestPackage
                ? badgeFor(JOB_PACKAGE_STATES, newestPackage.status)
                : null
              const packageListing = ownPackage ? null : (listed?.listing.member ?? null)
              const hiddenSelected = listings.filter(
                (l) => l.job !== null && selected.has(l.member.job_id),
              ).length
              const role = [job.job_id, ...listings.map((l) => l.member.job_id)]
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
                    data-posting={job.job_id}
                    data-control="select"
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
                      data-posting={job.job_id}
                      data-control="link"
                    >
                      {job.title}
                    </a>
                    <p className="truncate text-sm text-ink-3" title={meta}>
                      {meta}
                    </p>
                    {listings.length > 0 && (
                      <details
                        className="text-sm"
                        open={role.some((id) => openListings.has(id))}
                        onToggle={(e) => showListings(role, e.currentTarget.open)}
                      >
                        <summary
                          className="cursor-pointer text-ink-3"
                          data-posting={role.join(' ')}
                          data-control="listings"
                        >
                          {listings.length === 1
                            ? 'Also listed once more'
                            : `Also listed ${listings.length} more times`}
                          {hiddenSelected > 0 && (
                            <span className="text-ink-2">
                              {' '}
                              · {hiddenSelected === 1 ? 'one' : hiddenSelected} of them selected
                            </span>
                          )}
                        </summary>
                        <ul className="mt-1 space-y-1">
                          {listings.map(({ member, job: listedJob }) => {
                            const memberPackage = jobPackage.get(member.job_id)
                            const memberBadge = memberPackage
                              ? badgeFor(JOB_PACKAGE_STATES, memberPackage.status)
                              : null
                            const seen = listedOn(member.first_seen_at)
                            return (
                              <li key={member.job_id} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                                {listedJob && (
                                  <input
                                    type="checkbox"
                                    checked={selected.has(member.job_id)}
                                    disabled={listStale}
                                    aria-label={`Select the listing first seen ${seen}`}
                                    data-posting={member.job_id}
                                    data-control="select"
                                    onChange={() => toggleSelected(member.job_id)}
                                  />
                                )}
                                <a
                                  href={member.url}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="text-ink-2 underline hover:text-ink"
                                  data-posting={member.job_id}
                                  data-control="link"
                                >
                                  Listing first seen {seen}
                                </a>
                                <span className="text-ink-3">
                                  {member.in_run ? 'in this search' : 'from an earlier search'}
                                </span>
                                {memberBadge && (
                                  <span
                                    className={`rounded-full px-2 py-0.5 text-xs font-medium ${chip[memberBadge.tone]}`}
                                  >
                                    {memberBadge.label}
                                  </span>
                                )}
                              </li>
                            )
                          })}
                        </ul>
                      </details>
                    )}
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
                    {packageBadge && (
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-medium ${chip[packageBadge.tone]}`}
                      >
                        {packageBadge.label}
                      </span>
                    )}
                    {packageListing && (
                      <span className="text-xs text-ink-3">
                        for the listing first seen {listedOn(packageListing.first_seen_at)}
                      </span>
                    )}
                    <button
                      className={`${btnSecondary} rounded-md px-2 py-1 text-xs`}
                      disabled={busyDetail === job.job_id}
                      aria-expanded={expanded === job.job_id}
                      data-posting={job.job_id}
                      data-control="details"
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
                    <div className="col-span-full mt-1 space-y-2" data-posting={job.job_id}>
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
                      {job.requirements && job.requirements.length > 0 && (
                        <RequirementChecklist items={job.requirements} />
                      )}
                      {job.description && (
                        // Text, never markup: the posting is someone
                        // else's words, and only its paragraph and line
                        // breaks are kept.
                        <div className="space-y-2 rounded-lg bg-surface-2 p-3 text-sm text-ink-2">
                          {paragraphs(job.description).map((lines, i) => (
                            <p key={i} className="whitespace-pre-line">
                              {lines.join('\n')}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
        {selectedVisible.length > 0 && (
          // Gate 3, kept in reach: sticky to the bottom of the viewport
          // while the list runs past it, back in its own place once the
          // end of the list is on screen — so it never hides the last
          // rows the way a fixed bar would. Bleeds to the card's edges.
          <div
            ref={barRef}
            className="sticky bottom-0 -mx-4 -mb-4 mt-3 flex flex-wrap items-center gap-3 rounded-b-xl border-t border-line bg-surface px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] sm:-mx-6 sm:-mb-6 sm:px-6"
          >
            <span className="text-sm text-ink-2" aria-live="polite">
              {selectedVisible.length} selected
            </span>
            <button
              className={`${btnPrimary} rounded-md px-4 py-2 text-sm font-medium`}
              disabled={busyGenerate || listStale}
              onClick={generatePackages}
              title="Tailored resume + cover letter PDFs for each selected job"
            >
              {busyGenerate
                ? 'Queueing…'
                : `Generate packages (${selectedVisible.length})`}
            </button>
            <button
              className={`${btnSecondary} rounded-md px-4 py-2 text-sm`}
              onClick={() => {
                setSelected(new Set())
                setGenerateError(null)
                land(jobsHeadingRef)
              }}
            >
              Clear selection
            </button>
            {generateError && (
              <span role="alert" className="text-sm text-danger-fg">
                {generateError}
              </span>
            )}
          </div>
        )}
      </section>
    </div>
  )
}

/** What the posting asks for, a line each, and whether the profile has
 * it — by the rules the score used, so the two never disagree. */
function RequirementChecklist({ items }: { items: Requirement[] }) {
  return (
    <div className="rounded-lg bg-surface-2 p-3 text-sm">
      <h3 className="text-xs uppercase tracking-wide text-ink-3">Requirements</h3>
      {REQUIREMENT_GROUPS.map(([heading, belongs]) => {
        const group = items.filter(belongs)
        if (group.length === 0) return null
        return (
          <div key={heading} className="mt-2">
            <h4 className="text-xs font-medium text-ink-2">{heading}</h4>
            <ul className="mt-0.5 grid gap-x-4 sm:grid-cols-2">
              {group.map((item) => (
                <li
                  key={item.label}
                  className={
                    item.met === true
                      ? 'text-ok-fg'
                      : item.met === false && item.kind !== 'preferred'
                        ? 'text-warn-fg'
                        : 'text-ink-2'
                  }
                >
                  <span aria-hidden="true">
                    {item.met === true ? '✓' : item.met === false ? '✗' : '?'}
                  </span>{' '}
                  <span className="sr-only">
                    {item.met === true ? 'Has:' : item.met === false ? 'Missing:' : 'Unknown:'}
                  </span>{' '}
                  {item.label}
                </li>
              ))}
            </ul>
          </div>
        )
      })}
    </div>
  )
}
