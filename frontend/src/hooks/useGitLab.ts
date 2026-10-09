import { useQuery } from "@tanstack/react-query";
import { fetchDiscoverableProjects } from "@/api/gitlab";

/** #169: token-visible GitLab projects minus the registry, for Add-repo. */
export function useDiscoverableProjects() {
  return useQuery({
    queryKey: ["gitlab", "discoverable-projects"],
    queryFn: () => fetchDiscoverableProjects(),
    staleTime: 5 * 60_000,
  });
}
