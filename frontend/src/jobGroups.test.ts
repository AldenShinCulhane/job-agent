import test from 'node:test'
import assert from 'node:assert/strict'
import { newRoleCount, roleCount, roleRows, selectableIds } from './jobGroups.ts'
import type { GroupMember, Job } from './api.ts'

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

// An earlier search's A, and this search's B and C, all one role.
const MEMBERS = [
  member('a', '2026-08-01T00:00:00Z', false),
  member('b', '2026-09-01T00:00:00Z', true),
  member('c', '2026-09-02T00:00:00Z', true),
]
const group = { id: 'a', members: MEMBERS }
const B = job('b', { first_seen_at: MEMBERS[1]!.first_seen_at, group })
const C = job('c', { first_seen_at: MEMBERS[2]!.first_seen_at, group })
const SOLO = job('solo')
const none = () => false

test('postings of one role are one row, in the place of its first posting', () => {
  const rows = roleRows([B, SOLO, C], none)
  assert.deepEqual(rows.map((r) => r.job.job_id), ['c', 'solo'])
  assert.deepEqual(rows[0]!.listings.map((l) => [l.member.job_id, l.job?.job_id ?? null]),
    [['a', null], ['b', 'b']])
  assert.deepEqual(rows[1]!.listings, [])
})

test('the row stands for a posting with its own package, then the newest', () => {
  assert.equal(roleRows([B, C], none)[0]!.job.job_id, 'c')
  assert.equal(roleRows([B, C], (id) => id === 'b')[0]!.job.job_id, 'b')
  assert.equal(roleRows([C, B], (id) => id === 'b')[0]!.job.job_id, 'b')
})

test('an earlier listing is never the row, even when it has the package', () => {
  const rows = roleRows([B], (id) => id === 'a')
  assert.equal(rows[0]!.job.job_id, 'b')
  assert.deepEqual(selectableIds(rows), ['b'])
})

test('filters judge postings: a filtered-out posting is listed but cannot be selected', () => {
  const rows = roleRows([B], none) // C did not pass the filters
  assert.equal(rows[0]!.job.job_id, 'b')
  assert.deepEqual(rows[0]!.listings.map((l) => [l.member.job_id, l.job]), [['a', null], ['c', null]])
  assert.deepEqual(selectableIds(roleRows([B, C], none)), ['c', 'b'])
})

test('counts are roles: the listings of one role count once', () => {
  assert.equal(roleCount([B, SOLO, C]), 2)
  assert.equal(roleCount([]), 0)
})

test('a role is new when its listings are, and counts once', () => {
  const fresh = { id: 'b', members: MEMBERS.slice(1) }
  const jobs = [
    job('b', { group: fresh, novelty: 'new' }),
    job('c', { group: fresh, novelty: 'new' }),
    job('solo', { novelty: 'new' }),
    job('again', { novelty: null }),
    job('repost', { novelty: 'reposted' }),
    job('unread'),
  ]
  assert.equal(newRoleCount(jobs), 2)
  assert.equal(newRoleCount([B, C]), 0)
})
