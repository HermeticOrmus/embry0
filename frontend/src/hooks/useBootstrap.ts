import { useMutation, useQueryClient } from "@tanstack/react-query";
import { bootstrapRepo, type BootstrapRequest } from "@/api/bootstrap";

export function useBootstrapRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (req: BootstrapRequest) => bootstrapRepo(req),
    onSuccess: (result) => {
      const [owner, name] = result.repo.split("/");
      qc.invalidateQueries({ queryKey: ["qa-config", owner, name] });
      qc.invalidateQueries({ queryKey: ["repo-preferences", owner, name] });
      // QA dashboard repo lists don't know about the new repo yet.
      qc.invalidateQueries({ queryKey: ["qa-dashboard"] });
      // Bootstrap registers the repo (qa_configs upsert auto-registers), so
      // the registry list and the discoverable set both changed (RAV-1301).
      qc.invalidateQueries({ queryKey: ["repos"] });
      qc.invalidateQueries({ queryKey: ["github", "discoverable-repos"] });
    },
  });
}
