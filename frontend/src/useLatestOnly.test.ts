import test from 'node:test'
import assert from 'node:assert/strict'
import { latestOnly } from './useLatestOnly.ts'

// Every answer is released by hand, so ordering is the test's decision
// and nothing here waits on a clock.
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

test('the newest read wins whichever answer lands last', async () => {
  const read = latestOnly()
  const first = deferred<string>()
  const second = deferred<string>()
  const applied: string[] = []
  const p1 = read(() => first.promise, (v) => applied.push(v))
  const p2 = read(() => second.promise, (v) => applied.push(v))
  second.resolve('new')
  first.resolve('old')
  assert.deepEqual(await Promise.all([p1, p2]), [false, true])
  assert.deepEqual(applied, ['new'])
})

test('a paced read stands down while the newest read is still out', async () => {
  const read = latestOnly()
  const slow = deferred<string>()
  const applied: string[] = []
  let issued = 0
  const p1 = read(() => slow.promise, (v) => applied.push(v))
  const paced = await read(
    () => {
      issued += 1
      return Promise.resolve('tick')
    },
    (v) => applied.push(v),
    { ifIdle: true },
  )
  assert.equal(paced, false)
  assert.equal(issued, 0)
  slow.resolve('slow')
  assert.equal(await p1, true)
  assert.deepEqual(applied, ['slow'])
})

test('a prompted read supersedes a pending paced read', async () => {
  const read = latestOnly()
  const pacedAnswer = deferred<string>()
  const applied: string[] = []
  const paced = read(() => pacedAnswer.promise, (v) => applied.push(v), { ifIdle: true })
  const prompted = read(() => Promise.resolve('event'), (v) => applied.push(v))
  assert.equal(await prompted, true)
  pacedAnswer.resolve('stale')
  assert.equal(await paced, false)
  assert.deepEqual(applied, ['event'])
})

test('a rejected paced read releases the slot for the next tick', async () => {
  const read = latestOnly()
  const applied: string[] = []
  await assert.rejects(
    read(() => Promise.reject(new Error('down')), () => {}, { ifIdle: true }),
    /down/,
  )
  const next = await read(() => Promise.resolve('up'), (v) => applied.push(v), { ifIdle: true })
  assert.equal(next, true)
  assert.deepEqual(applied, ['up'])
})

test('a superseded rejection is dropped rather than surfaced', async () => {
  const read = latestOnly()
  const first = deferred<string>()
  const applied: string[] = []
  const p1 = read(() => first.promise, (v) => applied.push(v))
  const p2 = read(() => Promise.resolve('new'), (v) => applied.push(v))
  assert.equal(await p2, true)
  first.reject(new Error('too late to matter'))
  assert.equal(await p1, false)
  assert.deepEqual(applied, ['new'])
})

test('round 14: ten paced ticks during one slow read issue nothing and it still lands', async () => {
  const read = latestOnly()
  const answers: ReturnType<typeof deferred<number>>[] = []
  const applied: number[] = []
  const fetcher = () => {
    const d = deferred<number>()
    answers.push(d)
    return d.promise
  }
  const results = [read(fetcher, (v) => applied.push(v))]
  for (let i = 0; i < 10; i++) {
    results.push(read(fetcher, (v) => applied.push(v), { ifIdle: true }))
  }
  assert.equal(answers.length, 1)
  answers[0]!.resolve(1)
  const settled = await Promise.all(results)
  assert.deepEqual(applied, [1])
  assert.equal(settled[0], true)
  assert.ok(settled.slice(1).every((s) => s === false))
  // And once it has landed, the next tick is free to go.
  const after = read(fetcher, (v) => applied.push(v), { ifIdle: true })
  answers[1]!.resolve(2)
  assert.equal(await after, true)
})
