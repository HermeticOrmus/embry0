# OpenTofu modules

Generic modules for the parts of an embry0 host that live outside the Compose
stack:

| Module | Replaces |
|---|---|
| [`modules/cloudflare-tunnel`](modules/cloudflare-tunnel/README.md) | `infra/cloudflared-dashboard/provision.sh`, and the dashboard clicks that create the webhooks tunnel and its path restriction |
| [`modules/proxmox-vm`](modules/proxmox-vm/README.md) | The `qm` steps of [docs/ops/provisioning.md](../../docs/ops/provisioning.md#2-create-the-vm) §2 |

They are an alternative to the manual steps, which keep working. The Compose
stack itself, its images and its `.env` files stay with Compose and
`infra/deploy.sh`; nothing here manages them.

## Your root module lives elsewhere

These modules hold no deployment's values: every hostname, account, zone,
node and size is a variable without a default. Keep your root module, its
variables and its state in a private repository of your own, not in this
tree. The repository's `.gitignore` refuses `*.tfvars`, `*.tfstate*`,
`*.tfplan` and `.terraform/` here as a backstop.

State holds the tunnel secret and run token. Use a backend with access
control and encryption, such as an S3-compatible bucket (MinIO included) with
OpenTofu's state encryption, kept separate from the stack's own buckets and
credentials, or `pg`. Commit your root's `.terraform.lock.hcl` there.

[`examples/`](examples/) shows a root that calls all three: the dashboard
tunnel, the webhooks tunnel and the host, with placeholder values in
`terraform.tfvars.example`. Copy it out of the tree before filling it in.

## Provider versions

Both are pinned exactly in each module. A bump is a deliberate change: edit
the version, then rerun the checks below.

| Provider | Version | Why exact |
|---|---|---|
| `cloudflare/cloudflare` | 5.27.0 | v5 renamed most resources and still moves schemas between minors |
| `bpg/proxmox` | 0.116.0 | Never below 0.93.1 (CVE-2026-25499) |

## Checks

Against mocked providers, with no credentials:

```bash
tofu fmt -check -recursive infra/tofu
for d in infra/tofu/modules/* infra/tofu/examples; do
  (cd "$d" && tofu init -backend=false -input=false >/dev/null && tofu validate) || exit 1
done
(cd infra/tofu/modules/cloudflare-tunnel && tofu test)
(cd infra/tofu/modules/proxmox-vm && tofu test)
(cd infra/tofu/examples && tofu test -var-file=terraform.tfvars.example)
```

CI runs the same steps.
