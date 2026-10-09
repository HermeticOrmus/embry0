import { useQuery } from "@tanstack/react-query";
import { fetchAuditLog, type AuditFilters } from "@/api/audit";

export function useAuditLog(filters: AuditFilters) {
  return useQuery({
    queryKey: ["audit", filters],
    queryFn: () => fetchAuditLog(filters),
    refetchInterval: 30_000,
  });
}
