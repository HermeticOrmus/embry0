import { api } from "./client";
import type { LogEvent } from "@/lib/types/events";
import type { LogsContainersResponse, LogsQueryParams, LogsQueryResponse } from "@/lib/types";

export async function fetchJobLogEvents(jobId: string): Promise<LogEvent[]> {
  const { data } = await api.get(`/jobs/${jobId}/logs/events`);
  return data.events;
}

/** RAV-1295: the set of containers Loki is collecting (for the dropdown). */
export async function fetchLogContainers(): Promise<LogsContainersResponse> {
  const { data } = await api.get<LogsContainersResponse>("/logs/containers");
  return data;
}

/** RAV-1057: on-demand container-log query via the orchestrator's Loki proxy. */
export async function queryLogs(params: LogsQueryParams): Promise<LogsQueryResponse> {
  const { data } = await api.get<LogsQueryResponse>("/logs/query", { params });
  return data;
}
