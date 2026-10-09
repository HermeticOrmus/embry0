import type { JSX } from "react";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/Tabs";
import { useQaAttempts, useQaServices } from "@/hooks/useQaResults";
import { QAAttemptCard } from "./QAAttemptCard";
import { QAScreenshotPanel } from "./QAScreenshotPanel";
import { QALiveLogTail } from "./QALiveLogTail";

interface Props {
  jobId: string;
  jobIsLive: boolean;
}

export function QATab({ jobId, jobIsLive }: Props): JSX.Element {
  const { data: attempts, isLoading } = useQaAttempts(jobId, jobIsLive);
  const { data: services } = useQaServices(jobId, jobIsLive);

  if (isLoading) {
    return <p className="text-muted-foreground">Loading QA attempts...</p>;
  }
  if (!attempts || attempts.length === 0) {
    return (
      <div className="text-center py-12 text-white/40">
        <p>No QA attempts yet.</p>
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <div className="lg:col-span-2 space-y-4">
        {attempts.map((a) => (
          <QAAttemptCard key={a.attempt_n} jobId={jobId} attempt={a} />
        ))}
      </div>
      <div className="space-y-4">
        <div>
          <h3 className="text-sm text-white/60 mb-2">Live screenshot</h3>
          <QAScreenshotPanel jobId={jobId} jobIsLive={jobIsLive} />
        </div>
        {jobIsLive && services && services.length > 0 && (
          // RAV-1058: tabs come from the workspace's real compose service
          // list (GET /jobs/{id}/qa/services) — empty means the workspace
          // isn't up yet, so no tabs render. An "All logs" tab stays out of
          // scope: the backend's _SAFE_SERVICE regex rejects empty names.
          <Tabs defaultValue={services[0]} key={services.join(",")}>
            <TabsList>
              {services.map((s) => (
                <TabsTrigger key={s} value={s}>
                  {s}
                </TabsTrigger>
              ))}
            </TabsList>
            {services.map((s) => (
              <TabsContent key={s} value={s}>
                <QALiveLogTail jobId={jobId} service={s} active={jobIsLive} />
              </TabsContent>
            ))}
          </Tabs>
        )}
      </div>
    </div>
  );
}
