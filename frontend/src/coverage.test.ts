import test from 'node:test'
import assert from 'node:assert/strict'
import { coverageSummary, isMentioned, mentionPlaces } from './coverage.ts'
import type { SkillMention } from './api.ts'

function skill(label: string, over: Partial<SkillMention> = {}): SkillMention {
  return { kind: 'required', label, listed: false, resume: false, letter: false, ...over }
}

test('a skill is mentioned when any document has it', () => {
  assert.equal(isMentioned(skill('Python', { listed: true })), true)
  assert.equal(isMentioned(skill('Kafka', { resume: true })), true)
  assert.equal(isMentioned(skill('Kafka', { letter: true })), true)
  assert.equal(isMentioned(skill('Kafka')), false)
  // Text a short name isn't searched in is no mention.
  assert.equal(isMentioned(skill('Go', { resume: null, letter: null })), false)
  assert.equal(isMentioned(skill('Go', { listed: true, resume: null, letter: null })), true)
})

test('where a skill is mentioned, and what was not searched', () => {
  assert.equal(mentionPlaces(skill('Python', { listed: true, resume: true, letter: true })),
    'skills, resume text, cover letter')
  assert.equal(mentionPlaces(skill('Kafka', { letter: true })), 'cover letter')
  assert.equal(mentionPlaces(skill('Kafka')), 'not mentioned')
  assert.equal(mentionPlaces(skill('Go', { listed: true, resume: null, letter: null })),
    'skills; text not searched (short name)')
  assert.equal(mentionPlaces(skill('R', { resume: null, letter: null })),
    'not in skills; text not searched (short name)')
})

test('the summary counts mentions per kind, and no coverage is never none', () => {
  const coverage = [
    skill('Python', { listed: true }), skill('Kafka', { resume: true }), skill('Go', {
      resume: null, letter: null,
    }),
    skill('Docker', { kind: 'preferred', letter: true }), skill('Rust', { kind: 'preferred' }),
  ]
  assert.equal(coverageSummary(coverage), 'Mentions 2 of 3 required and 1 of 2 preferred skills')
  assert.equal(coverageSummary(coverage.slice(0, 3)), 'Mentions 2 of 3 required skills')
  assert.equal(coverageSummary(coverage.slice(3)), 'Mentions 1 of 2 preferred skills')
  assert.equal(coverageSummary([]), 'The posting names no skills')
  assert.equal(coverageSummary(null), 'Keyword mentions unavailable')
  assert.equal(coverageSummary(undefined), 'Keyword mentions unavailable')
})
