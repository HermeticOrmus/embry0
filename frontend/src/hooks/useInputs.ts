import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { fetchJobInputs, answerInput } from "@/api/inputs";

export function useJobInputs(jobId: string) {
  return useQuery({
    queryKey: ["job-inputs", jobId],
    queryFn: () => fetchJobInputs(jobId),
    enabled: !!jobId,
    refetchInterval: 5000,
  });
}

export function useAnswerInput() {
  const qc = useQueryClient();
  return useMutation({
    // jobId is threaded through so issue-less jobs (issue_id empty) answer via
    // the job-scoped endpoint instead of throwing in answerInput (RAV-1059).
    mutationFn: ({ issueId, jobId, inputId, answer }: { issueId: string; jobId: string; inputId: string; answer: string }) =>
      answerInput(issueId, inputId, answer, jobId),
    onSuccess: (_, { jobId }) => qc.invalidateQueries({ queryKey: ["job-inputs", jobId] }),
  });
}
