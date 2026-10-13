/** "acme/a, acme/b" or one per line -> ["acme/a", "acme/b"]; blank -> []. */
export function parseRepoList(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}
