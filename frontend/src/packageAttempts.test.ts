import test from 'node:test'
import assert from 'node:assert/strict'
import {
  attemptLabel, failedFirst, newestByJob, newestPerJob, requestKey, retryAction,
  truthCheckSummary,
} from './packageAttempts.ts'
import type { Package, UnsupportedClaim } from './api.ts'

function pkg(over: Partial<Package>): Package {
  return {
    id: 1, job_id: 'a', profile_version: 4, jd_hash: 'h', page_limit: 1,
    attempt: 1, retry_of: null, inputs_changed: [], status: 'done', error: null,
    retry_count: 0, output_dir: null, created_at: '', updated_at: '',
    title: 'T', company: 'C', url: '', provenance: {}, ...over,
  }
}

test('only each job\'s newest package offers a retry', () => {
  const list = [
    pkg({ id: 9, job_id: 'a', status: 'failed' }),
    pkg({ id: 8, job_id: 'b', status: 'done' }),
    pkg({ id: 7, job_id: 'a', status: 'failed' }),
    pkg({ id: 6, job_id: 'c', status: 'generating' }),
  ]
  const newest = newestPerJob(list)
  assert.deepEqual([...newest], [9, 8, 6])
  assert.deepEqual(list.map((p) => retryAction(p, newest)),
    ['Try again', 'Regenerate', null, null])
})

test('a retried row says whether it is another attempt or a new package', () => {
  assert.equal(attemptLabel(pkg({})), null)
  assert.equal(attemptLabel(pkg({ retry_of: 3, attempt: 2 })), 'Attempt 2')
  assert.equal(attemptLabel(pkg({ retry_of: 3, inputs_changed: ['profile'] })),
    'New package: the profile changed since the one you retried')
  assert.equal(
    attemptLabel(pkg({ retry_of: 3, inputs_changed: ['profile', 'posting', 'templates'] })),
    'New package: the profile, posting and templates changed since the one you retried')
})

test('each click gets its own plain key', () => {
  const a = requestKey()
  const b = requestKey()
  assert.notEqual(a, b)
  for (const key of [a, b]) assert.match(key, /^[A-Za-z0-9_-]{8,64}$/)
})

test('a failure still waiting on the user comes first; an answered one keeps its place', () => {
  const list = [
    pkg({ id: 9, job_id: 'a', status: 'done' }),
    pkg({ id: 8, job_id: 'b', status: 'failed' }),
    pkg({ id: 7, job_id: 'a', status: 'failed' }),
    pkg({ id: 6, job_id: 'c', status: 'generating' }),
    pkg({ id: 5, job_id: 'd', status: 'failed' }),
  ]
  assert.deepEqual(failedFirst(list, newestPerJob(list)).map((p) => p.id), [8, 5, 9, 7, 6])
})

test("each job's newest package", () => {
  const list = [
    pkg({ id: 9, job_id: 'a', status: 'queued' }),
    pkg({ id: 8, job_id: 'b', status: 'done' }),
    pkg({ id: 7, job_id: 'a', status: 'failed' }),
  ]
  const newest = newestByJob(list)
  assert.deepEqual([...newest.entries()].map(([job, p]) => [job, p.id]), [['a', 9], ['b', 8]])
})

test('only claims the profile does not support are counted as such', () => {
  const claim: UnsupportedClaim = {
    kind: 'claim', document: 'resume', claim: 'Led a team.', reason: 'no fact' }
  const structure: UnsupportedClaim = {
    kind: 'structure', document: 'resume', claim: null, reason: 'not a permutation' }
  assert.equal(truthCheckSummary([claim]), 'a claim your profile doesn’t support')
  assert.equal(truthCheckSummary([claim, claim]), '2 claims your profile doesn’t support')
  assert.equal(truthCheckSummary([structure]), 'a validation issue')
  assert.equal(truthCheckSummary([claim, structure]), '2 validation issues')
})
