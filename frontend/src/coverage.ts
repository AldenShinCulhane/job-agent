import type { SkillMention } from './api'

/** Whether the package's documents mention the skill anywhere. */
export function isMentioned(skill: SkillMention): boolean {
  return skill.listed || skill.resume === true || skill.letter === true
}

/** Where the documents mention a skill, in the page's words. A short
 * name is looked for in the skills only, and says so. */
export function mentionPlaces(skill: SkillMention): string {
  if (skill.resume === null) {
    return `${skill.listed ? 'skills' : 'not in skills'}; text not searched (short name)`
  }
  const places = [
    skill.listed && 'skills',
    skill.resume && 'resume text',
    skill.letter && 'cover letter',
  ].filter((place): place is string => typeof place === 'string')
  return places.length > 0 ? places.join(', ') : 'not mentioned'
}

/** The package row's line: how many of the posting's skills its
 * documents mention. No coverage is unknown, never none. */
export function coverageSummary(coverage: SkillMention[] | null | undefined): string {
  if (coverage == null) return 'Keyword mentions unavailable'
  if (coverage.length === 0) return 'The posting names no skills'
  const parts = (['required', 'preferred'] as const).flatMap((kind) => {
    const stated = coverage.filter((skill) => skill.kind === kind)
    return stated.length > 0
      ? [`${stated.filter(isMentioned).length} of ${stated.length} ${kind}`]
      : []
  })
  return `Mentions ${parts.join(' and ')} skills`
}
