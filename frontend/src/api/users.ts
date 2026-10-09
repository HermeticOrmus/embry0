import { api } from "./client";

export interface AdminUser {
  id: string;
  auth0_sub: string;
  email: string;
  name: string;
  tier: "none" | "operator" | "admin";
  disabled: boolean;
  session_epoch: number;
  login_count: number;
  first_login_at: string | null;
  last_login_at: string | null;
  created_at: string | null;
}

export interface UsersResponse {
  users: AdminUser[];
  total: number;
}

export async function fetchUsers(limit = 100, offset = 0): Promise<UsersResponse> {
  const { data } = await api.get<UsersResponse>("/users", { params: { limit, offset } });
  return data;
}

export async function setUserDisabled(userId: string, disabled: boolean): Promise<AdminUser> {
  const { data } = await api.put<AdminUser>(`/users/${userId}/disabled`, { disabled });
  return data;
}

export async function forceLogoutUser(userId: string): Promise<AdminUser> {
  const { data } = await api.post<AdminUser>(`/users/${userId}/force-logout`);
  return data;
}
