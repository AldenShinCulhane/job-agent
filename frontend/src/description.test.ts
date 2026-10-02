import test from 'node:test'
import assert from 'node:assert/strict'
import { paragraphs } from './description.ts'

test('paragraphs and their lines', () => {
  assert.deepEqual(paragraphs('Intro line.\n\nBuild APIs.\nShip weekly.\n\n\nClosing.'), [
    ['Intro line.'],
    ['Build APIs.', 'Ship weekly.'],
    ['Closing.'],
  ])
})

test('an older description is one paragraph', () => {
  assert.deepEqual(paragraphs('Build APIs. Requirements: Python.'), [
    ['Build APIs. Requirements: Python.'],
  ])
})

test('stray whitespace makes no empty paragraph or line', () => {
  assert.deepEqual(paragraphs('  \n\nOne\n \nTwo\n  \n'), [['One'], ['Two']])
})
