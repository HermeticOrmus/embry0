import { api } from "./client";
import type { WatcherRun, WatcherTickResult } from "@/lib/types";

export async function fetchWatcherRuns(limit = 10): Promise<WatcherRun[]> {
  const { data } = await api.get<WatcherRun[]>("/watcher/runs", { params: { limit } });
  return data;
}

/**
 * Run one watcher tick synchronously. A tick that finds signal spawns an
 * analysis job inline, so this can take minutes — hence the long timeout
 * (the client default is 30 s).
 */
export async function triggerWatcherRun(): Promise<WatcherTickResult> {
  const { data } = await api.post<WatcherTickResult>("/watcher/run", undefined, {
    timeout: 300_000,
  });
  return data;
}
