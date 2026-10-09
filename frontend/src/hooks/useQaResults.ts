import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import {
  listQaAttempts,
  listQaServices,
  getQaResult,
  type QAAttemptListEntry,
  type QAResult,
} from "@/api/qa-artifacts";

export function useQaAttempts(
  jobId: string,
  jobIsLive: boolean,
): UseQueryResult<QAAttemptListEntry[]> {
  return useQuery<QAAttemptListEntry[]>({
    queryKey: ["qa-attempts", jobId],
    queryFn: () => listQaAttempts(jobId),
    refetchInterval: jobIsLive ? 5000 : false,
  });
}

/** Live QA workspace services (RAV-1058) — polls only while the job is live;
 * an empty list means the workspace isn't up (no tabs render). */
export function useQaServices(jobId: string, jobIsLive: boolean): UseQueryResult<string[]> {
  return useQuery<string[]>({
    queryKey: ["qa-services", jobId],
    queryFn: () => listQaServices(jobId),
    enabled: jobIsLive,
    refetchInterval: jobIsLive ? 15_000 : false,
  });
}

export function useQaResult(
  jobId: string,
  attemptN: number | null,
  hasResult: boolean,
): UseQueryResult<QAResult> {
  return useQuery<QAResult>({
    queryKey: ["qa-result", jobId, attemptN],
    queryFn: () => getQaResult(jobId, attemptN!),
    enabled: attemptN != null && hasResult,
  });
}
