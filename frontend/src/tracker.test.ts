import test from 'node:test'
import assert from 'node:assert/strict'
import { roleRows } from './jobGroups.ts'
import {
  changes, dayLabel, formValues, roleApplication, settled, today, unsaved,
} from './tracker.ts'
import type { Application, ApplicationStatus, GroupMember, Job } from './api.ts'

function job(id: string, over: Partial<Job> = {}): Job {
  return {
    job_id: id, title: 'Backend Engineer', company: 'Example Co', location: 'Toronto, ON',
    posted_text: null, workplace: null, easy_apply: false, url: `https://example.invalid/${id}`,
    first_seen_at: '2026-09-01T00:00:00Z', last_seen_at: '2026-09-01T00:00:00Z',
    description: null, description_hash: null, detail_fetched_at: null,
    deterministic_score: null, llm_rerank: null, llm_rank: null, score_breakdown: null,
    group: null, ...over,
  }
}

function member(id: string, firstSeen: string, inRun: boolean): GroupMember {
  return { job_id: id, url: `https://example.invalid/${id}`, first_seen_at: firstSeen, in_run: inRun }
}

function event(id: number, jobId: string, status: ApplicationStatus): [string, Application] {
  return [jobId, { event_id: id, job_id: jobId, package_id: null, status, occurred_on: '2026-10-01' }]
}

// An earlier search's A and this search's B, one role; and a role alone.
const group = { id: 'a', members: [member('a', '2026-08-01T00:00:00Z', false),
                                   member('b', '2026-09-01T00:00:00Z', true)] }
const [ROLE, SOLO] = roleRows([job('b', { group }), job('solo')], () => false)

test('a role shows the latest status any of its postings has', () => {
  // A applied first, B withdrew later: B's is the latest event.
  const found = roleApplication(ROLE!, new Map([event(3, 'a', 'applied'), event(7, 'b', 'withdrawn')]))
  assert.equal(found?.application.status, 'withdrawn')
  assert.equal(found?.listing, null)
})

test("another listing's status is named as that listing's", () => {
  const found = roleApplication(ROLE!, new Map([event(3, 'a', 'interviewing')]))
  assert.equal(found?.application.status, 'interviewing')
  assert.equal(found?.listing?.job_id, 'a')
})

test('a role no posting of which is tracked shows nothing', () => {
  assert.equal(roleApplication(ROLE!, new Map([event(3, 'solo', 'applied')])), null)
  assert.equal(roleApplication(SOLO!, new Map([event(3, 'solo', 'applied')]))?.listing, null)
})

test("a form shows the posting's status and its day until the user changes them", () => {
  const [, current] = event(4, 'a', 'interviewing')
  assert.deepEqual(formValues(undefined, current, '2026-10-16'),
                   { status: 'interviewing', day: '2026-10-01' })
  // A new status happened today, unless the user says when.
  assert.deepEqual(formValues({ status: 'offer' }, current, '2026-10-16'),
                   { status: 'offer', day: '2026-10-16' })
  assert.deepEqual(formValues({ status: 'interviewing' }, current, '2026-10-16'),
                   { status: 'interviewing', day: '2026-10-01' })
  assert.deepEqual(formValues({ status: 'offer', day: '2026-09-28' }, current, '2026-10-16'),
                   { status: 'offer', day: '2026-09-28' })
  assert.deepEqual(formValues({ day: '2026-09-28' }, current, '2026-10-16'),
                   { status: 'interviewing', day: '2026-09-28' })
  // Cleared elsewhere: the draft is all there is.
  assert.deepEqual(formValues({ status: 'offer' }, undefined, '2026-10-16'),
                   { status: 'offer', day: '2026-10-16' })
})

test('a form changed back to what the posting has is no edit', () => {
  const [, current] = event(4, 'a', 'interviewing')
  assert.equal(changes({ status: 'interviewing' }, current, '2026-10-16'), false)
  assert.equal(changes({ status: 'interviewing', day: '2026-10-01' }, current, '2026-10-16'), false)
  assert.equal(changes({ status: 'offer' }, current, '2026-10-16'), true)
  assert.equal(changes({ day: '2026-09-28' }, current, '2026-10-16'), true)
  assert.equal(changes({ status: 'interviewing' }, undefined, '2026-10-16'), true)
})

test('a saved draft goes once the tracker is read back; an unsaved one stays', () => {
  const none = { 8: { status: 'offer' as const } }
  assert.equal(unsaved(none), none)
  assert.deepEqual(unsaved({ 8: { status: 'offer', saved: true }, 10: { day: '2026-10-03' } }),
                   { 10: { day: '2026-10-03' } })
})

test('a form edited during a save follows the status it was edited against', () => {
  const [, before] = event(4, 'a', 'interviewing')
  const [, after] = event(5, 'a', 'offer')
  const edited = { status: 'interviewing' as const, basis: before }
  // The save brought an offer; the change back still shows its own day.
  assert.deepEqual(formValues(edited, after, '2026-10-16'),
                   { status: 'interviewing', day: '2026-10-01' })
  assert.equal(changes(edited, { ...after, occurred_on: '2026-10-16' }, '2026-10-16'), true)
  // Edited while untracked: nothing to follow but the draft.
  assert.deepEqual(formValues({ day: '2026-09-28', basis: null }, after, '2026-10-16'),
                   { status: 'applied', day: '2026-09-28' })
})

test('a save settled: the forms edited during it are judged by the read', () => {
  const [, read] = event(5, 'a', 'offer')
  const tracker = new Map([['a', read]])
  const drafts = {
    8: { status: 'offer' as const, day: '2026-10-01', basis: null },  // what the save made
    10: { status: 'interviewing' as const, basis: read },  // still a change
    12: { status: 'offer' as const, day: '2026-10-01' },  // not edited during it
  }
  assert.deepEqual(settled(drafts, [[8, 'a'], [10, 'a']], tracker, '2026-10-16'),
                   { 10: drafts[10], 12: drafts[12] })
  assert.equal(settled(drafts, [[10, 'a'], [14, 'a']], tracker, '2026-10-16'), drafts)
})

// West of Greenwich, where a bare date read as UTC midnight is the day
// before. Each test file runs in a process of its own.
process.env.TZ = 'America/Los_Angeles'

test('a day is a day on the calendar, in any time zone', () => {
  assert.equal(today(new Date(2026, 9, 4, 23, 59)), '2026-10-04')
  assert.equal(today(new Date(2026, 0, 1, 0, 0)), '2026-01-01')
  assert.equal(dayLabel('2026-10-04'),
               new Date(2026, 9, 4).toLocaleDateString([], { month: 'short', day: 'numeric' }))
  assert.equal(dayLabel('not a day'), 'not a day')
})
