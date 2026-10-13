import { api } from "./client";

export interface QAReadyCheckResult {
  url: string;
  status: number;
  duration_ms: number;
}

export interface QABootResult {
  command: string;
  duration_ms: number;
  ready_checks: QAReadyCheckResult[];
}

export interface QASeedResult {
  ran: boolean;
  command?: string;
  duration_ms?: number;
  exit_code?: number;
  note?: string;
}

export interface QAE2EResult {
  ran: boolean;
  command?: string;
  exit_code?: number;
  tests?: Record<string, number>;
  junit_artifact?: string;
}

export interface QAAcceptanceResult {
  criterion: string;
  status: "passed" | "failed" | "inconclusive";
  evidence: string[];
  notes?: string;
  console_errors: string[];
  network_failures: { url: string; status: number }[];
  log_excerpts: { service: string; lines: string }[];
}

export interface QAAnomaly {
  category: "console_error" | "network_error" | "unexpected_state" | "crash" | "guardrail_violation";
  detail: string;
  evidence_paths: string[];
  ts?: string;
}

export interface QAResult {
  schema_version: 1;
  job_id: string;
  attempt_n: number;
  phase_reached: "boot" | "seed" | "e2e" | "exploratory" | "report";
  overall: "passed" | "failed" | "inconclusive";
  boot: QABootResult;
  seed?: QASeedResult;
  e2e?: QAE2EResult;
  acceptance_results: QAAcceptanceResult[];
  anomalies: QAAnomaly[];
}

export interface QAAttemptListEntry {
  attempt_n: number;
  has_result_json: boolean;
  screenshots_count: number;
}

export async function listQaAttempts(jobId: string): Promise<QAAttemptListEntry[]> {
  const { data } = await api.get<{ attempts: QAAttemptListEntry[] }>(
    `/jobs/${jobId}/qa/attempts`,
  );
  return data.attempts;
}

/** Compose services of the job's live QA workspace — feeds the live-log
 * service tabs. Empty while the workspace is down. */
export async function listQaServices(jobId: string): Promise<string[]> {
  const { data } = await api.get<{ services: string[] }>(`/jobs/${jobId}/qa/services`);
  return data.services;
}

export async function getQaResult(jobId: string, attemptN: number): Promise<QAResult> {
  const { data } = await api.get<QAResult>(
    `/jobs/${jobId}/qa/attempts/${attemptN}/result`,
  );
  return data;
}

function encodePath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

/**
 * axios path (no `/api/v1` prefix) to the bytes of one artifact of one
 * attempt, e.g. an acceptance criterion's evidence file.
 *
 * Artifact bytes always come through the orchestrator (#541): the
 * `/jobs/{id}/artifacts/...` routes answer with a 302 to a presigned GET on
 * the storage endpoint, which is compose-internal and unreachable from a
 * browser. Those redirects are for sandbox and agent callers only. Fetch
 * these paths with `api.get(..., { responseType: "blob" })` so the Bearer
 * header is attached, then hand the browser a blob: URL.
 */
export function attemptArtifactPath(jobId: string, attemptN: number, path: string): string {
  return `/jobs/${encodeURIComponent(jobId)}/qa/attempts/${attemptN}/artifacts/${encodePath(path)}`;
}

/** axios path to the bytes of the job's newest screenshot (any attempt). */
export function latestScreenshotPath(jobId: string): string {
  return `/jobs/${encodeURIComponent(jobId)}/qa/screenshots/latest`;
}

/** Fetch artifact bytes through the authenticated client as a Blob. */
export async function fetchArtifactBlob(path: string): Promise<Blob> {
  const { data } = await api.get<Blob>(path, { responseType: "blob" });
  return data;
}

/**
 * Mint a short-lived token that opens this job's live log stream (#634).
 * EventSource cannot send an Authorization header, so the stream URL carries
 * this job-scoped token instead — never the API key.
 */
export async function mintLogStreamToken(jobId: string): Promise<string> {
  const { data } = await api.post<{ token: string }>(`/jobs/${jobId}/qa/log-stream-token`);
  return data.token;
}

export function logStreamUrl(jobId: string, service: string, token: string): string {
  return `/api/v1/jobs/${jobId}/artifacts/logs/${encodeURIComponent(service)}?follow=true&token=${encodeURIComponent(token)}`;
}
