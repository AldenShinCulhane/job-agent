/** Which usage-limit episodes have already opened the engine picker in
 * this browser tab, so a limit opens it once — not on every countdown
 * tick, reconnect, or reload. Kept for the tab's session; storage that is
 * blocked or broken falls back to memory for the page's lifetime. */

const KEY = 'job-agent.engine-episodes-shown'
const KEEP = 20
const memory = new Set<string>()

function stored(): string[] {
  try {
    const parsed: unknown = JSON.parse(globalThis.sessionStorage?.getItem(KEY) ?? '[]')
    return Array.isArray(parsed) ? parsed.filter((x): x is string => typeof x === 'string') : []
  } catch {
    return []
  }
}

export function episodeShown(id: string): boolean {
  return memory.has(id) || stored().includes(id)
}

export function markEpisodeShown(id: string): void {
  memory.add(id)
  try {
    const list = [...stored().filter((x) => x !== id), id].slice(-KEEP)
    globalThis.sessionStorage?.setItem(KEY, JSON.stringify(list))
  } catch {
    /* memory still has it */
  }
}
