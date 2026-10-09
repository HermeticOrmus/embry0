# The VM of docs/ops/provisioning.md §2, as one resource. In qm terms:
#
#   qm create --cores N --sockets 1 --cpu host --memory M --balloon B
#     --net0 virtio,bridge=<bridge> --scsihw virtio-scsi-single --ostype l26
#     --agent enabled=1 --onboot 1 --serial0 socket --vga std
#     --ipconfig0 ip=dhcp --cicustom user=<snippet>
#   qm disk import + qm set --scsi0 + qm disk resize   -> disk { import_from }
#   qm set --ide2 <storage>:cloudinit                  -> initialization
#   qm set --boot order=scsi0                          -> boot_order
#   qm start                                           -> started
#
# Everything here goes through the Proxmox API. The provider needs SSH only
# for snippet uploads and the legacy `file_id` import, neither of which this
# module uses.

resource "proxmox_virtual_environment_vm" "this" {
  node_name = var.node_name
  vm_id     = var.vm_id
  name      = var.name
  tags      = var.tags

  on_boot = true
  started = true

  cpu {
    cores   = var.cores
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = var.memory_mb
    floating  = var.balloon_min_mb
  }

  operating_system {
    type = "l26"
  }

  agent {
    enabled = true
  }

  serial_device {
    device = "socket"
  }

  vga {
    type = "std"
  }

  scsi_hardware = "virtio-scsi-single"

  network_device {
    bridge = var.network_bridge
    model  = "virtio"
  }

  disk {
    interface    = "scsi0"
    datastore_id = var.datastore_id
    import_from  = var.disk_image_file_id
    size         = var.disk_size_gb
  }

  boot_order = ["scsi0"]

  initialization {
    interface         = "ide2"
    datastore_id      = var.datastore_id
    user_data_file_id = var.user_data_file_id

    ip_config {
      ipv4 {
        address = var.ipv4_address
        gateway = var.ipv4_gateway
      }
    }
  }

  lifecycle {
    # The image only seeds the disk on create. A newer image later must not
    # replace a running host, and a VM imported from one built by hand has
    # no import source recorded.
    ignore_changes = [disk[0].import_from]

    precondition {
      condition     = var.balloon_min_mb <= var.memory_mb
      error_message = "balloon_min_mb must not exceed memory_mb."
    }
    precondition {
      condition     = var.ipv4_address != "dhcp" || var.ipv4_gateway == null
      error_message = "ipv4_gateway must be null when ipv4_address is \"dhcp\"."
    }
  }
}
