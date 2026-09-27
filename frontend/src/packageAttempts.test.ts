import test from 'node:test'
import assert from 'node:assert/strict'
import { attemptLabel, newestPerJob, requestKey, retryAction } from './packageAttempts.ts'
import type { Package } from './api.ts'

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
