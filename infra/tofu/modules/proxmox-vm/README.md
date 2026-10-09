# proxmox-vm

The embry0 host VM of [docs/ops/provisioning.md](../../../../docs/ops/provisioning.md#2-create-the-vm)
§2 as one OpenTofu resource, in place of its `qm create`, `qm disk import`,
`qm set` and `qm disk resize` steps. The manual steps still work, and the
rest of the runbook (CA trust, env files, the stack) is unchanged.

Provider: `bpg/proxmox` pinned at exactly `0.116.0`. Never use a version below
0.93.1, which fixes CVE-2026-25499. The module configures no provider; your
root module does, with `PROXMOX_VE_ENDPOINT` and `PROXMOX_VE_API_TOKEN` in the
environment.

## No SSH, no sudo

Everything goes through the Proxmox API: the disk is imported with
`import_from`, and cloud-init points at a snippet that already exists. The
provider opens SSH only for snippet uploads, the legacy `file_id` import and
LXC idmaps, none of which this module uses, so its provider block needs no
`ssh` settings. Do not grant the provider's SSH user `qm` or `pvesm` through
sudo: either is root on the hypervisor (GHSA-m8h8-vrx7-4hqx, unpatched).

## Before you apply

1. **The image**, as a volume of content type `import`. Enable `import` on the
   storage (`pvesm set local --content iso,vztmpl,backup,snippets,import`, adding
   to what it already has), then place the Ubuntu cloud image under
   `/var/lib/vz/import/`, renamed to `.qcow2` because that is its real format.
   `disk_image_file_id` is then `local:import/<file>.qcow2`.
2. **The cloud-init snippet**, as in the runbook:
   `scp infra/cloud-init/embry0-user.yaml.example <hypervisor>:/var/lib/vz/snippets/embry0-user.yaml`,
   with your hostname and keys filled in. `user_data_file_id` is then
   `local:snippets/embry0-user.yaml`.
3. **A free VMID**: `qm config <vmid>` should say the file does not exist.

## What it sets

| Runbook (`qm`) | Here |
|---|---|
| `--cores N --sockets 1 --cpu host` | `cores`; one socket and type `host` are fixed |
| `--memory M --balloon B` | `memory_mb`, `balloon_min_mb` (the floor; must not exceed `memory_mb`) |
| `--net0 virtio,bridge=X` | `network_bridge`; model `virtio` fixed |
| `--scsihw virtio-scsi-single --ostype l26 --agent enabled=1 --onboot 1 --serial0 socket --vga std` | fixed |
| `--ipconfig0 ip=dhcp` | `ipv4_address` (`"dhcp"` by default) and `ipv4_gateway` |
| `--cicustom user=...` | `user_data_file_id` |
| `qm disk import` + `--scsi0` + `qm disk resize` | `disk_image_file_id`, `datastore_id`, `disk_size_gb` |
| `--ide2 <storage>:cloudinit` | the cloud-init drive on `ide2`, in `datastore_id` |
| `--boot order=scsi0`, `qm start` | fixed |

## Inputs

| Name | Type | Default | Description |
|---|---|---|---|
| `node_name` | string | required | Proxmox node |
| `vm_id` | number | required | VMID, 100 to 999999999 |
| `name` | string | required | One DNS label |
| `cores` | number | required | vCPU cores |
| `memory_mb` | number | required | Maximum memory, MiB |
| `balloon_min_mb` | number | required | Balloon floor, MiB |
| `disk_size_gb` | number | required | Boot disk, GiB |
| `datastore_id` | string | required | Storage for the disk and the cloud-init drive |
| `disk_image_file_id` | string | required | `<storage>:import/<file>.qcow2` (or `.raw`, `.vmdk`) |
| `user_data_file_id` | string | required | `<storage>:snippets/<file>` |
| `network_bridge` | string | required | Bridge for the one NIC |
| `ipv4_address` | string | `"dhcp"` | `"dhcp"` or a CIDR address |
| `ipv4_gateway` | string | `null` | Gateway for a static address |
| `tags` | list(string) | `[]` | Proxmox tags |

There are no size defaults: VM sizes are an operator's configuration. The
example root uses the runbook's 8 cores, 16 GiB with a 4 GiB floor, and 150 GB.

## Outputs

`vm_id`, `name`, `ipv4_addresses` and `mac_addresses`. The addresses come from
the guest agent, which answers only after cloud-init's first-boot package
install.

## Importing an existing VM

```hcl
import {
  to = module.host.proxmox_virtual_environment_vm.this
  id = "<node_name>/<vmid>"
}
```

The disk's import source is ignored after create, so a VM built by hand, or a
newer image later, never plans a replacement. Plan against your host and
reconcile any remaining difference in your variables before applying.

## Tests

```bash
tofu init -backend=false
tofu test
```

The provider is mocked; nothing contacts Proxmox.
