import test from 'node:test'
import assert from 'node:assert/strict'
import { editAt } from './entries.ts'

test('an existing entry is changed in place', () => {
  const list = [{ title: 'a' }, { title: 'b' }]
  assert.equal(editAt(list, 1, (e) => (e.title = 'B')), true)
  assert.deepEqual(list, [{ title: 'a' }, { title: 'B' }])
})

test('a missing entry is left missing: nothing invented, nothing thrown', () => {
  const list = [{ title: 'a' }]
  let called = 0
  assert.equal(editAt(list, 1, () => { called += 1 }), false)
  assert.equal(editAt(list, -1, () => { called += 1 }), false)
  assert.equal(editAt([], 0, () => { called += 1 }), false)
  assert.equal(called, 0)
  assert.deepEqual(list, [{ title: 'a' }])
})
