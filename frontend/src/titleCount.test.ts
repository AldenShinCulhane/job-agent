import test from 'node:test'
import assert from 'node:assert/strict'
import { baselineOf, finishedSince, titleFor } from './titleCount.ts'
import type { Package } from './api.ts'

function pkg(id: number, status: string): Package {
  return {
    id, job_id: `job-${id}`, profile_version: 4, jd_hash: 'h', page_limit: 1,
    attempt: 1, retry_of: null, inputs_changed: [], status, error: null,
    retry_count: 0, output_dir: null, created_at: '', updated_at: '',
    title: 'T', company: 'C', url: '', provenance: {},
  }
}

const SHOWN = [pkg(1, 'done'), pkg(2, 'generating'), pkg(3, 'queued'), pkg(4, 'failed')]

test('what finished since the baseline counts, done or failed', () => {
  const baseline = baselineOf(SHOWN)
  assert.equal(finishedSince(baseline, SHOWN), 0)
  assert.equal(finishedSince(baseline, [pkg(1, 'done'), pkg(2, 'done'), pkg(3, 'failed'),
    pkg(4, 'failed')]), 2)
})

test('a package the baseline never listed counts once it has finished', () => {
  const baseline = baselineOf(SHOWN)
  assert.equal(finishedSince(baseline, [...SHOWN, pkg(5, 'done')]), 1)
  assert.equal(finishedSince(baseline, [...SHOWN, pkg(5, 'fitting')]), 0)
  assert.equal(finishedSince(baselineOf([]), [pkg(5, 'failed')]), 1)
})

test('still working, or waiting on a usage limit, is not finished', () => {
  const baseline = baselineOf(SHOWN)
  assert.equal(finishedSince(baseline, [pkg(2, 'publishing'), pkg(3, 'waiting_usage')]), 0)
})

test('a package is counted by its id, once', () => {
  const baseline = baselineOf(SHOWN)
  assert.equal(finishedSince(baseline, [pkg(2, 'done'), pkg(2, 'done')]), 1)
})

test('the title counts what finished, and says when the count is at least', () => {
  assert.equal(titleFor(3, false), '(3) Job Agent')
  assert.equal(titleFor(1, true), '(1+) Job Agent')
  assert.equal(titleFor(0, false), 'Job Agent')
  assert.equal(titleFor(0, true), 'Job Agent')
})
