// Mirrors the statuses the backend actually writes (issue_inputs repo):
// pending -> answered | auto_answered | skipped. An overridden auto-answer is
// status "answered" with auto_answer still set (RAV-1059) — not a separate state.
export type InputStatus = "pending" | "answered" | "auto_answered" | "skipped";

export interface JobInput {
  input_id: string;
  job_id: string;
  issue_id: string;
  question: string;
  category: string;
  options: string[] | null;
  status: InputStatus;
  answer: string | null;
  auto_answer: string | null;
  created_at: string;
  answered_at: string | null;
}

export type InputImportance = "blocking" | "auto_answerable";
export type IssueInputStatus = "pending" | "auto_answered" | "answered";

export interface InputResponse {
  id: string;
  issue_id: string;
  job_id: string;
  asking_node: string;
  question: string;
  importance: InputImportance;
  auto_answer: string | null;
  answer: string | null;
  answered_by: string | null;
  status: IssueInputStatus;
  created_at: string;
  answered_at: string | null;
}
