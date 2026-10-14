import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// Records are operator-written but still untrusted text: react-markdown
// renders no raw HTML unless a rehype-raw plugin is added, and none is.
// Its default urlTransform also drops javascript: and other unsafe hrefs.

const components: Components = {
  h1: ({ children }) => <h1 className="mb-3 mt-6 text-2xl font-bold first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-2 mt-6 text-xl font-semibold">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-2 mt-4 text-lg font-semibold">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-2 mt-4 font-semibold">{children}</h4>,
  p: ({ children }) => <p className="my-3 leading-relaxed text-white/80">{children}</p>,
  ul: ({ children }) => <ul className="my-3 list-disc space-y-1 pl-6 text-white/80">{children}</ul>,
  ol: ({ children }) => <ol className="my-3 list-decimal space-y-1 pl-6 text-white/80">{children}</ol>,
  a: ({ href, children }) => (
    <a href={href} className="text-cyan-400 hover:underline" target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  blockquote: ({ children }) => (
    <blockquote className="my-3 border-l-2 border-white/20 pl-4 text-white/60">{children}</blockquote>
  ),
  pre: ({ children }) => (
    <pre className="my-3 overflow-x-auto rounded-md bg-black/40 p-3 font-mono text-xs">{children}</pre>
  ),
  code: ({ className, children }) => (
    <code className={className ? `${className} font-mono` : "rounded bg-white/10 px-1 py-0.5 font-mono text-[0.85em]"}>
      {children}
    </code>
  ),
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border border-white/10 px-2 py-1 text-left font-semibold">{children}</th>,
  td: ({ children }) => <td className="border border-white/10 px-2 py-1 align-top">{children}</td>,
  hr: () => <hr className="my-6 border-white/10" />,
};

export function RecordMarkdown({ body }: { body: string }) {
  return (
    <div className="text-sm" data-testid="record-markdown">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {body}
      </Markdown>
    </div>
  );
}
