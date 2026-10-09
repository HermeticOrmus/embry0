import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchRepoPreferences,
  setRepoPreferences,
  deleteRepoPreferences,
  type RepoPreferencesUpdate,
} from "@/api/repoPreferences";

export function useRepoPreferences(fullRepo: string) {
  return useQuery({
    queryKey: ["repo-preferences", fullRepo],
    queryFn: () => fetchRepoPreferences(fullRepo),
    enabled: !!fullRepo,
  });
}

export function useSetRepoPreferences() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      fullRepo,
      update,
    }: {
      fullRepo: string;
      update: RepoPreferencesUpdate;
    }) => setRepoPreferences(fullRepo, update),
    onSuccess: (_data, { fullRepo }) =>
      qc.invalidateQueries({ queryKey: ["repo-preferences", fullRepo] }),
  });
}

export function useDeleteRepoPreferences() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ fullRepo }: { fullRepo: string }) =>
      deleteRepoPreferences(fullRepo),
    onSuccess: (_data, { fullRepo }) =>
      qc.invalidateQueries({ queryKey: ["repo-preferences", fullRepo] }),
  });
}
