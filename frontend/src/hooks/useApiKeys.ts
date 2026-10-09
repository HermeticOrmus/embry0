import { useQuery } from "@tanstack/react-query";
import { fetchApiKeys } from "@/api/apiKeys";

export function useApiKeys() {
  return useQuery({
    queryKey: ["api-keys"],
    queryFn: fetchApiKeys,
  });
}
