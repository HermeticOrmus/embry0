import { isAxiosError } from "axios";
import { api } from "./client";

export interface QaConfigSaveResult {
  repo: string;
  updated_at: string;
}

/**
 * Fetch the stored qa.yaml for a repo as raw YAML text (RAV-1251: the
 * embry0 DB is the source of truth).
 *
 * Returns null on 404 — no config is stored for the repo. Legacy in-repo
 * `.embry0/qa.yaml` files are a deprecated import channel handled by the
 * orchestrator and deliberately invisible to this API.
 */
export async function fetchQaConfig(fullRepo: string): Promise<string | null> {
  try {
    const { data } = await api.get<string>(`/repos/${fullRepo}/qa-config`, {
      responseType: "text",
      // Identity transform — without it axios tries JSON.parse on the YAML.
      transformResponse: [(d: string) => d],
    });
    return data;
  } catch (error) {
    if (isAxiosError(error) && error.response?.status === 404) return null;
    throw error;
  }
}

/**
 * Save qa.yaml v2 text to the store. The backend schema-validates the body
 * and rejects with 422 (flat string detail, often a multi-line Pydantic
 * report) — the caller renders that next to the editor, so the global
 * validation toast is suppressed.
 */
export async function saveQaConfig(
  fullRepo: string,
  yamlText: string,
): Promise<QaConfigSaveResult> {
  const { data } = await api.put<QaConfigSaveResult>(
    `/repos/${fullRepo}/qa-config`,
    yamlText,
    {
      headers: { "Content-Type": "application/yaml" },
      suppressValidationToast: true,
    },
  );
  return data;
}

/** Remove the stored qa.yaml (idempotent 204). QA then treats the repo as unconfigured. */
export async function deleteQaConfig(fullRepo: string): Promise<void> {
  await api.delete(`/repos/${fullRepo}/qa-config`);
}
