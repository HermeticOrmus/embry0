import { useEffect, useState } from "react";
import { isAxiosError } from "axios";
import { fetchArtifactBlob, latestScreenshotPath } from "@/api/qa-artifacts";

const POLL_MS = 5000;

/**
 * Returns a blob: URL for the job's latest screenshot, or `null` while there
 * is none.
 *
 * The bytes are fetched through the orchestrator with the authenticated axios
 * client (#541) — a bare `<img src="/api/v1/...">` would neither carry the
 * Bearer header nor survive the old 302 to the compose-internal storage
 * endpoint. While `jobIsLive` is true the screenshot is refetched every 5s;
 * otherwise it is fetched once. Each new frame replaces the previous blob
 * URL, which is revoked, and the last one is revoked on unmount or when the
 * job changes. A 404 means "no screenshot yet"; any other failure keeps the
 * last good frame until the next poll.
 */
export function useLatestScreenshot(jobId: string, jobIsLive: boolean): string | null {
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let current: string | null = null;
    let inFlight = false;

    const replace = (next: string | null) => {
      if (current) URL.revokeObjectURL(current);
      current = next;
      setUrl(next);
    };

    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const blob = await fetchArtifactBlob(latestScreenshotPath(jobId));
        if (cancelled) return;
        replace(URL.createObjectURL(blob));
      } catch (e) {
        if (cancelled) return;
        if (isAxiosError(e) && e.response?.status === 404) replace(null);
      } finally {
        inFlight = false;
      }
    };

    void load();
    const interval = jobIsLive ? setInterval(() => void load(), POLL_MS) : null;
    return () => {
      cancelled = true;
      if (interval) clearInterval(interval);
      if (current) URL.revokeObjectURL(current);
      current = null;
      setUrl(null);
    };
  }, [jobId, jobIsLive]);

  return url;
}
