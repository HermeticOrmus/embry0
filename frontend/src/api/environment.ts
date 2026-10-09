import { api } from "./client";
import type { EnvVar, DetectedEnvVar } from "@/lib/types/environment";

export async function fetchGlobalEnv(): Promise<EnvVar[]> {
  const { data } = await api.get("/environment/global");
  return data.variables;
}

export async function setGlobalEnv(variables: EnvVar[]): Promise<void> {
  await api.put("/environment/global", { variables });
}

export async function fetchRepoEnv(fullRepo: string): Promise<EnvVar[]> {
  const { data } = await api.get(`/repos/${fullRepo}/environment`);
  return data.variables;
}

export async function setRepoEnv(fullRepo: string, variables: EnvVar[]): Promise<void> {
  await api.put(`/repos/${fullRepo}/environment`, { variables });
}

export async function deleteRepoEnvVar(fullRepo: string, key: string): Promise<void> {
  await api.delete(`/repos/${fullRepo}/environment/${key}`);
}

export async function revealSecret(
  scope: "global" | "repo",
  key: string,
  fullRepo?: string,
): Promise<string> {
  const path =
    scope === "global"
      ? `/environment/global/${key}/reveal`
      : `/repos/${fullRepo}/environment/${key}/reveal`;
  const { data } = await api.get(path);
  return data.value;
}

export interface DetectRepoEnvResponse {
  source_file: string;
  variables: DetectedEnvVar[];
  unconfigured_count: number;
}

export async function detectRepoEnv(fullRepo: string): Promise<DetectRepoEnvResponse> {
  const { data } = await api.get(`/repos/${fullRepo}/environment/detect`);
  return data;
}
