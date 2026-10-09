import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { acceptCase, fetchProposals, rejectCase } from "@/api/cases";

const PROPOSALS_KEY = ["cases", "awaiting-decision"];

export function useProposals() {
  return useQuery({
    queryKey: PROPOSALS_KEY,
    queryFn: fetchProposals,
    refetchInterval: 30_000,
  });
}

export function useAcceptProposal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (caseId: string) => acceptCase(caseId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: PROPOSALS_KEY });
      // Accepting creates the issue row the list below shows.
      qc.invalidateQueries({ queryKey: ["issues"] });
    },
  });
}

export function useRejectProposal() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (caseId: string) => rejectCase(caseId),
    onSuccess: () => qc.invalidateQueries({ queryKey: PROPOSALS_KEY }),
  });
}
