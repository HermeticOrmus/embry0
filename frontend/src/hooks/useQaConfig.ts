import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchQaConfig, saveQaConfig, deleteQaConfig } from "@/api/qaConfig";

/**
 * The stored qa.yaml text for a repo. `data === null` means the external
 * store has no entry (the in-repo `.embry0/qa.yaml` fallback still applies
 * at run time — it just isn't editable here).
 */
export function useQaConfig(fullRepo: string) {
  return useQuery({
    queryKey: ["qa-config", fullRepo],
    queryFn: () => fetchQaConfig(fullRepo),
    enabled: !!fullRepo,
    retry: false,
  });
}

export function useSaveQaConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ fullRepo, yamlText }: { fullRepo: string; yamlText: string }) =>
      saveQaConfig(fullRepo, yamlText),
    onSuccess: (_data, { fullRepo }) =>
      qc.invalidateQueries({ queryKey: ["qa-config", fullRepo] }),
  });
}

export function useDeleteQaConfig() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ fullRepo }: { fullRepo: string }) =>
      deleteQaConfig(fullRepo),
    onSuccess: (_data, { fullRepo }) =>
      qc.invalidateQueries({ queryKey: ["qa-config", fullRepo] }),
  });
}
