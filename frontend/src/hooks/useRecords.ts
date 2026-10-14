import { useQuery } from "@tanstack/react-query";
import { fetchRecord, fetchRecordRevisions, fetchRecords, type RecordListFilters } from "@/api/records";

export function useRecords(repo: string, filters: RecordListFilters) {
  return useQuery({
    queryKey: ["records", repo, filters.kind ?? "", filters.q ?? ""],
    queryFn: () => fetchRecords(repo, filters),
    enabled: !!repo,
    retry: false,
  });
}

export function useRecord(repo: string, slug: string, revision?: number) {
  return useQuery({
    queryKey: ["records", repo, "detail", slug, revision ?? "current"],
    queryFn: () => fetchRecord(repo, slug, revision),
    enabled: !!repo && !!slug,
    retry: false,
  });
}

export function useRecordRevisions(repo: string, slug: string) {
  return useQuery({
    queryKey: ["records", repo, "revisions", slug],
    queryFn: () => fetchRecordRevisions(repo, slug),
    enabled: !!repo && !!slug,
    retry: false,
  });
}
