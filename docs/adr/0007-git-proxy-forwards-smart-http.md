# ADR 0007: The git-proxy forwards git's smart HTTP, so the forge token never reaches the sandbox

Status: accepted (2026-10-08)

A sandbox's git uses `git-proxy` as its remote: `url.<proxy>/git/<repo>.git.insteadOf <forge>/<repo>.git`, plus an `http.extraHeader` scoped to the proxy that carries the sandbox bearer. The proxy streams `info/refs`, `git-upload-pack` and `git-receive-pack` for the enrolled repository only (`allowed_repos`, as in #520), adds the forge credential to each upstream request, and allows receive-pack only to an enrollment with `git_push`, for the branches [ADR 0009](0009-git-proxy-scopes-push-to-the-jobs-branch.md) names. `/git-credentials` answers 410 and never returns a credential. The operator's credential therefore works for the job's repository and nothing else: private dependencies, submodules and LFS fetched with it stop working by design. Short-lived GitHub App installation tokens were set aside, because each operator would have to create an App and they do not cover GitLab. They remain a possible second layer.

Evidence: #521. `git_proxy.py` returned `password=<token>` to any holder of the sandbox bearer. This refines [ADR 0002](0002-credential-proxies-per-sandbox-enrollment.md) and reuses the scope from `ea8c5bb`.
