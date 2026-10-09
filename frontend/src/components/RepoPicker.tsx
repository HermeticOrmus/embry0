import { Select } from "@/components/ui/Select";
import { defaultRepoAnnotation, useRegisteredRepos } from "@/hooks/useRepos";
import type { RegisteredRepo } from "@/api/repos";

// RAV-1294: the shared repo selector. Replaces free-text owner/repo boxes
// with a picker fed by the repo registry (GET /repos), each entry annotated
// with its env-var footprint so an operator can see where configuration
// lives without knowing repo names by heart. RAV-1296 reuses this on
// Settings → Context.

interface RepoPickerProps {
  value: string;
  onChange: (repo: string) => void;
  /** Per-entry label; defaults to the env-var-count annotation. */
  annotate?: (r: RegisteredRepo) => string;
  /**
   * RAV-1296: repos to offer beyond the registry (e.g. repos that carry a
   * stored context but were never registered). Deduped against the registry.
   */
  extraRepos?: string[];
  /** Label for extraRepos entries; defaults to the bare repo name. */
  annotateExtra?: (repo: string) => string;
  placeholder?: string;
  id?: string;
  className?: string;
  disabled?: boolean;
}

export function RepoPicker({
  value,
  onChange,
  annotate = defaultRepoAnnotation,
  extraRepos = [],
  annotateExtra = (repo) => repo,
  placeholder = "Select a repository…",
  id,
  className,
  disabled,
}: RepoPickerProps) {
  const { data: repos = [], isLoading } = useRegisteredRepos();

  const registryNames = repos.map((r) => r.repo);
  const extras = [...new Set(extraRepos)].filter((r) => !registryNames.includes(r));
  // A deep-linked or previously-selected repo that is missing from the
  // registry must still be selectable so the page can render its state.
  const options = [...registryNames, ...extras];
  const orphanValue = value && !options.includes(value) ? value : null;

  return (
    <Select
      id={id}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={className}
      disabled={disabled || isLoading}
      aria-label="Repository"
    >
      <option value="">{isLoading ? "Loading repositories…" : placeholder}</option>
      {orphanValue && <option value={orphanValue}>{orphanValue} (unregistered)</option>}
      {repos.map((r) => (
        <option key={r.repo} value={r.repo}>
          {annotate(r)}
        </option>
      ))}
      {extras.map((repo) => (
        <option key={repo} value={repo}>
          {annotateExtra(repo)}
        </option>
      ))}
    </Select>
  );
}
