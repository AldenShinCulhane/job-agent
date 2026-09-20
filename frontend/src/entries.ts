/** Change the entry at `index` of `list` in place, if there is one.
 *
 * The profile editor maps over a list and hands each row a handler that
 * edits `list[i]`; between the render and the keystroke the row can be
 * gone (removed in another handler, replaced by a refetch), and an
 * assignment through an undefined entry would throw. A missing entry is
 * left missing and reported, not invented: the caller's next render will
 * not have the row either. */
export function editAt<T>(list: T[], index: number, change: (entry: T) => void): boolean {
  const entry = list[index]
  if (entry === undefined) return false
  change(entry)
  return true
}
