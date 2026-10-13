import { api } from "./client";

export interface ApiKey {
  id: string;
  user_id: string | null;
  name: string;
  prefix: string;
  scope: "read" | "operate" | "admin" | "merge";
  service: boolean;
  created_by: string;
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  /** Repositories the key may start work on; null = unrestricted. */
  allowed_repos: string[] | null;
}

export interface CreatedApiKey extends ApiKey {
  /** Full plaintext token — present ONLY in the create response. */
  key: string;
}

export async function fetchApiKeys(): Promise<ApiKey[]> {
  const { data } = await api.get<{ keys: ApiKey[] }>("/apikeys");
  return data.keys;
}

export async function createApiKey(body: {
  name: string;
  scope: string;
  expires_days?: number;
  service?: boolean;
  allowed_repos?: string[];
}): Promise<CreatedApiKey> {
  const { data } = await api.post<CreatedApiKey>("/apikeys", body);
  return data;
}

/** Replace a key's repository allowlist; an empty list lifts it. */
export async function setApiKeyAllowedRepos(
  id: string,
  allowedRepos: string[],
): Promise<ApiKey> {
  const { data } = await api.put<ApiKey>(`/apikeys/${id}/allowed-repos`, {
    allowed_repos: allowedRepos,
  });
  return data;
}

export async function revokeApiKey(id: string): Promise<ApiKey> {
  const { data } = await api.delete<ApiKey>(`/apikeys/${id}`);
  return data;
}
