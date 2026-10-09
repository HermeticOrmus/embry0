import { useQuery } from "@tanstack/react-query";
import { fetchUsers } from "@/api/users";

export function useUsers(limit = 100, offset = 0) {
  return useQuery({
    queryKey: ["admin-users", limit, offset],
    queryFn: () => fetchUsers(limit, offset),
    refetchInterval: 30_000,
  });
}
