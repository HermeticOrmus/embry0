import { useQuery } from "@tanstack/react-query";
import { fetchDiscoverableRepos, fetchGitHubRepos } from "@/api/github";

export function useGitHubRepos() {
  return useQuery({
    queryKey: ["github", "repos"],
    queryFn: fetchGitHubRepos,
    staleTime: 5 * 60_000,
  });
}

/** RAV-1301: token-visible repos minus the registry, for the Add-repo dropdown. */
export function useDiscoverableRepos() {
  return useQuery({
    queryKey: ["github", "discoverable-repos"],
    queryFn: () => fetchDiscoverableRepos(),
    staleTime: 5 * 60_000,
  });
}
