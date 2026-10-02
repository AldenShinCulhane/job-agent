/** A posting's paragraphs, each as its lines, from the stored text: a
 * blank line between paragraphs, a line break inside one (the backend
 * keeps both from the posting's markup). A description stored before
 * that is one paragraph of one line, as it always read. */
export function paragraphs(text: string): string[][] {
  return text
    .split(/\n\s*\n/)
    .map((block) => block.split('\n').map((line) => line.trim()).filter(Boolean))
    .filter((lines) => lines.length > 0)
}
