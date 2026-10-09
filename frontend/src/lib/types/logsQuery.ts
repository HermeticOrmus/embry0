/** RAV-1057 on-demand container-log queries — GET /logs/query (Loki proxy). */

export interface LogsQueryLine {
  ts: string;
  container: string;
  line: string;
}

export interface LogsQueryResponse {
  query: string;
  window_start: string;
  window_end: string;
  count: number;
  lines: LogsQueryLine[];
}

export interface LogsContainersResponse {
  containers: string[];
  cached: boolean;
}

export interface LogsQueryParams {
  /** RAV-1295: comma-separated exact container names (multi-select). */
  containers?: string;
  container?: string;
  search?: string;
  since_minutes?: number;
  limit?: number;
}
