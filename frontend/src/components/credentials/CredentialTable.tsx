import type { ReactNode } from "react";
import { KeyRound, Link2, Power, RefreshCw, ShieldCheck, X } from "lucide-react";
import { StatusBadge } from "@/components/credentials/StatusBadge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/EmptyState";
import { Tooltip } from "@/components/ui/Tooltip";
import type { Credential } from "@/lib/types/credentials";
import { cn, formatDate } from "@/lib/utils";

export interface CredentialTableProps {
  credentials: Credential[];
  isAdmin: boolean;
  onRotate(c: Credential): void;
  onVerify(c: Credential): void;
  onToggleDisabled(c: Credential): void;
  onDelete(c: Credential): void;
  onBindings(c: Credential): void;
  verifyingId?: string | null;
}

const HEADERS = ["Label", "Provider", "Preview", "Author", "Status", "Bindings", "Last used"] as const;

function authorLabel(credential: Credential): string {
  const name = credential.git_author_name;
  const email = credential.git_author_email;
  if (name && email) return `${name} <${email}>`;
  if (name) return name;
  if (email) return `<${email}>`;
  return "branding default";
}

/**
 * Mutating controls are disabled for non-admins. The tooltip sits on a
 * wrapper because a disabled button does not receive pointer events.
 */
function GuardedAction({
  isAdmin,
  label,
  testId,
  onClick,
  disabled = false,
  destructive = false,
  children,
}: {
  isAdmin: boolean;
  label: string;
  testId: string;
  onClick: () => void;
  disabled?: boolean;
  destructive?: boolean;
  children: ReactNode;
}) {
  const button = (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className={cn(
        "h-8 w-8 text-muted-foreground",
        destructive && "hover:bg-destructive/10 hover:text-destructive",
      )}
      data-testid={testId}
      aria-label={label}
      title={isAdmin ? label : undefined}
      disabled={!isAdmin || disabled}
      onClick={onClick}
    >
      {children}
    </Button>
  );
  if (isAdmin) return button;
  return <Tooltip content="Admin scope required">{button}</Tooltip>;
}

export function CredentialTable({
  credentials,
  isAdmin,
  onRotate,
  onVerify,
  onToggleDisabled,
  onDelete,
  onBindings,
  verifyingId,
}: CredentialTableProps) {
  if (credentials.length === 0) {
    return (
      <Card className="p-0">
        <EmptyState
          icon={KeyRound}
          title="No credentials yet"
          description="Run embry0 credentials import-env to import forge tokens from the orchestrator environment."
        />
      </Card>
    );
  }

  return (
    <Card className="p-0">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-white/[0.04] text-left text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
              {HEADERS.map((label) => (
                <th key={label} scope="col" className="px-4 py-3">
                  {label}
                </th>
              ))}
              <th scope="col" className="px-4 py-3 text-right">
                Actions
              </th>
            </tr>
          </thead>
          <tbody>
            {credentials.map((credential) => {
              const verifying = verifyingId === credential.id;
              const author = authorLabel(credential);
              return (
                <tr
                  key={credential.id}
                  data-testid="credential-row"
                  className="border-b border-white/[0.04] transition-colors hover:bg-white/[0.02]"
                >
                  <td className="px-4 py-3">
                    <div className="text-foreground">{credential.label}</div>
                    {credential.verified_login && (
                      <div className="text-xs text-muted-foreground">{credential.verified_login}</div>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <div>{credential.provider}</div>
                    <div className="text-xs text-muted-foreground">
                      {credential.provider === "anthropic" || credential.provider === "xai"
                        ? credential.kind
                        : credential.host}
                    </div>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-muted-foreground">
                    {credential.masked_preview}
                  </td>
                  <td
                    className={cn(
                      "px-4 py-3 text-xs",
                      author === "branding default" && "text-muted-foreground",
                    )}
                  >
                    {author}
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge credential={credential} />
                  </td>
                  <td className="px-4 py-3">
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="h-8 px-2 font-mono text-xs"
                      aria-label={`Show ${credential.binding_count} bindings`}
                      onClick={() => onBindings(credential)}
                    >
                      {credential.binding_count}
                    </Button>
                  </td>
                  <td className="whitespace-nowrap px-4 py-3 text-xs text-muted-foreground">
                    {credential.last_used_at ? formatDate(credential.last_used_at) : "never"}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex items-center justify-end gap-1">
                      <GuardedAction
                        isAdmin={isAdmin}
                        label="Rotate credential"
                        testId="credential-rotate"
                        onClick={() => onRotate(credential)}
                      >
                        <RefreshCw className="h-3.5 w-3.5" />
                      </GuardedAction>
                      <GuardedAction
                        isAdmin={isAdmin}
                        label={verifying ? "Verifying credential" : "Verify credential"}
                        testId="credential-verify"
                        disabled={verifying}
                        onClick={() => onVerify(credential)}
                      >
                        <ShieldCheck className="h-3.5 w-3.5" />
                      </GuardedAction>
                      <GuardedAction
                        isAdmin={isAdmin}
                        label={credential.status === "disabled" ? "Enable credential" : "Disable credential"}
                        testId="credential-toggle"
                        onClick={() => onToggleDisabled(credential)}
                      >
                        <Power className="h-3.5 w-3.5" />
                      </GuardedAction>
                      <GuardedAction
                        isAdmin={isAdmin}
                        label="Delete credential"
                        testId="credential-delete"
                        destructive
                        onClick={() => onDelete(credential)}
                      >
                        <X className="h-3.5 w-3.5" />
                      </GuardedAction>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-muted-foreground"
                        data-testid="credential-bindings"
                        aria-label="Show bindings"
                        title="Bindings"
                        onClick={() => onBindings(credential)}
                      >
                        <Link2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
