import { api } from "./client";
import type { ActivityEntry } from "@/lib/types";

export interface AuditFilters {
  actor?: string;
  action?: string;
  since?: string; // ISO datetime
  limit?: number;
  offset?: number;
}

export interface AuditResponse {
  entries: ActivityEntry[];
  total: number;
}

export async function fetchAuditLog(filters: AuditFilters = {}): Promise<AuditResponse> {
  const { data } = await api.get<AuditResponse>("/audit", { params: filters });
  return data;
}
