/**
 * Readable text from a FastAPI error `detail`. The field is a string when a
 * route raises `HTTPException(detail="...")`, and a list of pydantic error
 * objects (`{type, loc, msg, input}`) when request validation fails. Only
 * `loc` and `msg` are read: `input` is the rejected value and may be a
 * secret, so it is never copied into a message.
 */

/** Request parts FastAPI puts first in `loc`; the field path follows. */
const REQUEST_PARTS = new Set(["body", "query", "path", "header", "cookie"]);

function itemMessage(item: unknown, withField: boolean): string {
  if (typeof item === "string") return item.trim();
  if (!item || typeof item !== "object") return "";
  const rec = item as { loc?: unknown; msg?: unknown; message?: unknown };
  const raw = typeof rec.msg === "string" ? rec.msg : typeof rec.message === "string" ? rec.message : "";
  const msg = raw.trim();
  if (!msg || !withField || !Array.isArray(rec.loc)) return msg;
  const path = rec.loc.filter((part, index) => !(index === 0 && REQUEST_PARTS.has(String(part))));
  return path.length > 0 ? `${path.map(String).join(".")}: ${msg}` : msg;
}

function messages(detail: unknown, withField: boolean): string[] {
  const items = Array.isArray(detail) ? detail : [detail];
  return items.map((item) => itemMessage(item, withField)).filter(Boolean);
}

/** The trimmed messages of a detail, without field paths. */
export function detailMessages(detail: unknown): string[] {
  return messages(detail, false);
}

/**
 * One line for a detail: each pydantic error as `field: msg`, joined by
 * `; `. Null when the detail holds nothing readable.
 */
export function formatErrorDetail(detail: unknown): string | null {
  const parts = messages(detail, true);
  return parts.length > 0 ? parts.join("; ") : null;
}
