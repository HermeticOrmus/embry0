import { useQuery } from "@tanstack/react-query";
import { fetchWatcherRuns } from "@/api/watcher";

export function useWatcherRuns(limit = 10) {
  return useQuery({
    queryKey: ["watcher-runs", limit],
    queryFn: () => fetchWatcherRuns(limit),
    refetchInterval: 30_000,
  });
}
