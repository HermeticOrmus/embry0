import { api } from "./client";
import type {
  Binding,
  BindingInput,
  BindingRole,
  Credential,
  CredentialCreate,
  CredentialUpdate,
  ImportEnvReport,
  RepoCredentials,
  ResolveResult,
  ScopeKind,
  XaiDeviceLoginPoll,
  XaiDeviceLoginStarted,
} from "@/lib/types/credentials";

export async function fetchCredentials(): Promise<Credential[]> {
  const { data } = await api.get<Credential[]>("/credentials");
  return data;
}

export async function createCredential(body: CredentialCreate): Promise<Credential> {
  // Modal renders the 422 inline. Response is CredentialOut: masked_preview, never the secret.
  const { data } = await api.post<Credential>("/credentials", body, {
    suppressValidationToast: true,
  });
  return data;
}

export async function updateCredential(id: string, body: CredentialUpdate): Promise<Credential> {
  const { data } = await api.put<Credential>(`/credentials/${id}`, body);
  return data;
}

export async function rotateCredential(id: string, secret: string): Promise<Credential> {
  const { data } = await api.post<Credential>(
    `/credentials/${id}/rotate`,
    { secret },
    { suppressValidationToast: true },
  );
  return data;
}

export async function verifyCredential(id: string): Promise<Credential> {
  const { data } = await api.post<Credential>(`/credentials/${id}/verify`);
  return data;
}

export async function deleteCredential(id: string): Promise<void> {
  await api.delete(`/credentials/${id}`);
}

export async function resolveCredential(
  repo: string,
  role: BindingRole = "forge",
): Promise<ResolveResult> {
  const { data } = await api.get<ResolveResult>("/credentials/resolve", {
    params: { repo, role },
  });
  return data;
}

export async function fetchScopedBindings(
  scope_kind: ScopeKind,
  scope_value: string,
): Promise<Binding[]> {
  const { data } = await api.get<{ bindings: Binding[] }>("/credentials/bindings", {
    params: { scope_kind, scope_value },
  });
  return data.bindings;
}

export async function setScopedBindings(
  scope_kind: Exclude<ScopeKind, "repo">,
  scope_value: string,
  bindings: BindingInput[],
): Promise<Binding[]> {
  // ScopedBindingsSet puts scope in the body and rejects scope_kind "repo".
  // Repo bindings go through /repos/{repo}/credentials. The drawer renders a
  // 422 detail inline, so the global validation toast would duplicate it.
  const { data } = await api.put<{ bindings: Binding[] }>(
    "/credentials/bindings",
    { scope_kind, scope_value, bindings },
    { suppressValidationToast: true },
  );
  return data.bindings;
}

export async function fetchRepoCredentials(fullRepo: string): Promise<RepoCredentials> {
  // Slash stays in the path (`acme/widgets`); the route is `{full_repo:path}`.
  const { data } = await api.get<RepoCredentials>(`/repos/${fullRepo}/credentials`);
  return data;
}

export async function setRepoCredentials(
  fullRepo: string,
  bindings: BindingInput[],
): Promise<RepoCredentials> {
  const { data } = await api.put<RepoCredentials>(`/repos/${fullRepo}/credentials`, { bindings });
  return data;
}

export async function startXaiDeviceLogin(label: string): Promise<XaiDeviceLoginStarted> {
  const { data } = await api.post<XaiDeviceLoginStarted>(
    "/credentials/xai/device-login",
    { label },
    { suppressValidationToast: true },
  );
  return data;
}

export async function pollXaiDeviceLogin(loginId: string): Promise<XaiDeviceLoginPoll> {
  const { data } = await api.get<XaiDeviceLoginPoll>(`/credentials/xai/device-login/${loginId}`);
  return data;
}

export async function importEnvCredentials(replace: boolean): Promise<ImportEnvReport> {
  const { data } = await api.post<ImportEnvReport>("/credentials/import-env", { replace });
  return data;
}
