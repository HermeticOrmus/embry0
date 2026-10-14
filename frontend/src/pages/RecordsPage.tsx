import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { FileText } from "lucide-react";
import { RECORD_KINDS } from "@/api/records";
import { useRecord, useRecordRevisions, useRecords } from "@/hooks/useRecords";
import { RecordMarkdown } from "@/components/records/RecordMarkdown";
import { RepoPicker } from "@/components/RepoPicker";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { TableSkeleton } from "@/components/TableSkeleton";

/** Repo records: per-repository Markdown documents (RCAs, audits, specs,
 * plans, spikes, runbooks, notes) kept in embry0 rather than in a git tree.
 * Read-only here; operators write through the CLI, the API or MCP.
 * State lives in the query string (`repo`, `slug`, `rev`) so a view can be
 * bookmarked and shared. */
export function RecordsPage() {
  const [params, setParams] = useSearchParams();
  const repo = params.get("repo") ?? "";
  const slug = params.get("slug") ?? "";
  const revParam = Number(params.get("rev") ?? "");
  const rev = Number.isInteger(revParam) && revParam > 0 ? revParam : undefined;

  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");

  const update = (next: Record<string, string | undefined>) => {
    const merged = new URLSearchParams(params);
    for (const [k, v] of Object.entries(next)) {
      if (v) merged.set(k, v);
      else merged.delete(k);
    }
    setParams(merged);
  };

  const list = useRecords(repo, { kind, q: query.trim() });
  const record = useRecord(repo, slug, rev);
  const revisions = useRecordRevisions(repo, slug);

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-bold">Records</h1>
        <p className="mt-1 text-sm text-white/60">
          Documents kept per repository with full revision history: RCAs, audits, specs, plans, spikes,
          runbooks and notes. Agents working on a repository can read its records. Write them with{" "}
          <code className="font-mono">embry0 records</code> or the API.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <RepoPicker
          value={repo}
          onChange={(r) => update({ repo: r, slug: undefined, rev: undefined })}
          className="w-80"
        />
        {repo && (
          <Link
            to={`/repos/${repo}`}
            className="text-sm text-white/50 hover:underline"
            data-testid="records-repo-link"
          >
            Repo details
          </Link>
        )}
      </div>

      {!repo ? (
        <p className="text-sm text-muted-foreground">Choose a repository to see its records.</p>
      ) : (
        <div className="grid gap-6 lg:grid-cols-[22rem_1fr]">
          <Card className="space-y-3 p-4">
            <div className="flex gap-2">
              <Select
                aria-label="Kind"
                value={kind}
                onChange={(e) => setKind(e.target.value)}
                className="w-32"
              >
                <option value="">All kinds</option>
                {RECORD_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </Select>
              <Input
                aria-label="Search records"
                placeholder="Search title and text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            {list.isError ? (
              <p className="text-sm text-red-400">Could not load records for {repo}.</p>
            ) : list.isLoading || !list.data ? (
              <TableSkeleton />
            ) : list.data.records.length === 0 ? (
              <p className="text-sm text-muted-foreground" data-testid="records-empty">
                No records{kind || query ? " match" : " yet for this repository"}.
              </p>
            ) : (
              <ul className="space-y-1" data-testid="records-list">
                {list.data.records.map((r) => (
                  <li key={r.slug}>
                    <button
                      type="button"
                      onClick={() => update({ slug: r.slug, rev: undefined })}
                      className={`w-full rounded-md px-2 py-1.5 text-left text-sm hover:bg-white/5 ${
                        r.slug === slug ? "bg-white/10" : ""
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <Badge tone="neutral">{r.kind}</Badge>
                        <span className="truncate font-medium">{r.title}</span>
                      </div>
                      <div className="mt-0.5 text-xs text-white/40">
                        {r.slug} · rev {r.current_revision} · {r.updated_at}
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          <Card className="min-w-0 p-6">
            {!slug ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <FileText className="h-4 w-4" /> Select a record.
              </div>
            ) : record.isError ? (
              <p className="text-sm text-red-400" data-testid="record-missing">
                Record {slug} not found{rev ? ` at revision ${rev}` : ""}.
              </p>
            ) : record.isLoading || !record.data ? (
              <TableSkeleton />
            ) : (
              <div className="space-y-4">
                <div className="flex flex-wrap items-start justify-between gap-3 border-b border-white/10 pb-3">
                  <div>
                    <div className="flex items-center gap-2">
                      <Badge tone="neutral">{record.data.kind}</Badge>
                      <h2 className="text-lg font-semibold">{record.data.title}</h2>
                    </div>
                    <p className="mt-1 text-xs text-white/50" data-testid="record-meta">
                      {record.data.slug} · revision {record.data.revision} of {record.data.current_revision} by{" "}
                      {record.data.author} · {record.data.revision_created_at}
                      {record.data.source_job_id ? (
                        <>
                          {" · job "}
                          <Link to={`/jobs/${record.data.source_job_id}`} className="hover:underline">
                            {record.data.source_job_id}
                          </Link>
                        </>
                      ) : null}
                    </p>
                  </div>
                  <Select
                    aria-label="Revision"
                    value={String(rev ?? record.data.current_revision)}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      update({ rev: n === record.data.current_revision ? undefined : String(n) });
                    }}
                    className="w-56"
                  >
                    {(revisions.data?.revisions ?? [
                      { revision: record.data.current_revision, author: record.data.author, created_at: "" },
                    ]).map((r) => (
                      <option key={r.revision} value={String(r.revision)}>
                        {`rev ${r.revision}${r.revision === record.data.current_revision ? " (current)" : ""}${
                          r.created_at ? ` · ${r.created_at}` : ""
                        }`}
                      </option>
                    ))}
                  </Select>
                </div>
                <RecordMarkdown body={record.data.body} />
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}
