# ADR 0009: The git-proxy lets a push touch the job's own branch and nothing else

Status: accepted (2026-10-10)

The git-proxy reads the command list at the head of every `git-receive-pack` request before forwarding it. Each ref update must create or update `refs/heads/<b>` for a branch in the enrollment's `git_push_branches`; any other ref (another branch, the default branch, a tag) and any deletion refuse the whole push, with a per-ref report git prints, and nothing reaches the forge. The pack itself still streams through unread. The orchestrator enrolls the branch the job's state names, or its own, and swaps in the developer's choice through an admin route. The proxy cannot tell a fast-forward from a rewrite without the object graph, so a job may force-push its own branch. An enrollment without branches, and a signed push, are refused.

Evidence: #559, in which a developer agent force-pushed over another job's branch (#558). This refines [ADR 0007](0007-git-proxy-forwards-smart-http.md).
