# The VM matches docs/ops/provisioning.md §2. Mocked provider: nothing here
# talks to Proxmox.

mock_provider "proxmox" {}

variables {
  node_name          = "example-node"
  vm_id              = 900
  name               = "example-embry0"
  cores              = 8
  memory_mb          = 16384
  balloon_min_mb     = 4096
  disk_size_gb       = 150
  datastore_id       = "local-lvm"
  disk_image_file_id = "local:import/ubuntu-cloudimg-amd64.qcow2"
  user_data_file_id  = "local:snippets/embry0-user.yaml"
  network_bridge     = "vmbr0"
}

run "matches_the_runbook" {
  command = plan

  assert {
    condition = (
      proxmox_virtual_environment_vm.this.cpu[0].cores == 8 &&
      proxmox_virtual_environment_vm.this.cpu[0].sockets == 1 &&
      proxmox_virtual_environment_vm.this.cpu[0].type == "host"
    )
    error_message = "CPU is not N cores on one socket of type host."
  }

  assert {
    condition = (
      proxmox_virtual_environment_vm.this.memory[0].dedicated == 16384 &&
      proxmox_virtual_environment_vm.this.memory[0].floating == 4096
    )
    error_message = "Memory is not the maximum with the balloon floor."
  }

  assert {
    condition = (
      proxmox_virtual_environment_vm.this.disk[0].interface == "scsi0" &&
      proxmox_virtual_environment_vm.this.disk[0].datastore_id == "local-lvm" &&
      proxmox_virtual_environment_vm.this.disk[0].import_from == "local:import/ubuntu-cloudimg-amd64.qcow2" &&
      proxmox_virtual_environment_vm.this.disk[0].size == 150
    )
    error_message = "The boot disk is not the imported image on scsi0 at the requested size."
  }

  assert {
    condition     = jsonencode(proxmox_virtual_environment_vm.this.boot_order) == jsonencode(["scsi0"])
    error_message = "The VM does not boot from scsi0."
  }

  assert {
    condition = (
      proxmox_virtual_environment_vm.this.initialization[0].interface == "ide2" &&
      proxmox_virtual_environment_vm.this.initialization[0].datastore_id == "local-lvm" &&
      proxmox_virtual_environment_vm.this.initialization[0].user_data_file_id == "local:snippets/embry0-user.yaml" &&
      proxmox_virtual_environment_vm.this.initialization[0].ip_config[0].ipv4[0].address == "dhcp"
    )
    error_message = "The cloud-init drive is not on ide2 with the snippet and DHCP."
  }

  assert {
    condition = (
      proxmox_virtual_environment_vm.this.scsi_hardware == "virtio-scsi-single" &&
      proxmox_virtual_environment_vm.this.operating_system[0].type == "l26" &&
      proxmox_virtual_environment_vm.this.agent[0].enabled == true &&
      proxmox_virtual_environment_vm.this.serial_device[0].device == "socket" &&
      proxmox_virtual_environment_vm.this.vga[0].type == "std" &&
      proxmox_virtual_environment_vm.this.network_device[0].bridge == "vmbr0" &&
      proxmox_virtual_environment_vm.this.network_device[0].model == "virtio" &&
      proxmox_virtual_environment_vm.this.on_boot == true &&
      proxmox_virtual_environment_vm.this.started == true
    )
    error_message = "A fixed setting differs from the runbook's qm create."
  }
}

run "static_address" {
  command = plan

  variables {
    ipv4_address = "192.0.2.10/24"
    ipv4_gateway = "192.0.2.1"
  }

  assert {
    condition     = proxmox_virtual_environment_vm.this.initialization[0].ip_config[0].ipv4[0].gateway == "192.0.2.1"
    error_message = "The static gateway was not set."
  }
}

run "refuses_a_balloon_above_memory" {
  command = plan

  variables {
    balloon_min_mb = 32768
  }

  expect_failures = [proxmox_virtual_environment_vm.this]
}

run "refuses_a_gateway_with_dhcp" {
  command = plan

  variables {
    ipv4_gateway = "192.0.2.1"
  }

  expect_failures = [proxmox_virtual_environment_vm.this]
}

run "refuses_an_iso_volume" {
  # `import_from` takes only volumes of content type `import`.
  command = plan

  variables {
    disk_image_file_id = "local:iso/ubuntu-cloudimg-amd64.img"
  }

  expect_failures = [var.disk_image_file_id]
}

run "refuses_a_host_path_snippet" {
  command = plan

  variables {
    user_data_file_id = "/var/lib/vz/snippets/embry0-user.yaml"
  }

  expect_failures = [var.user_data_file_id]
}

run "refuses_a_name_with_dots" {
  command = plan

  variables {
    name = "embry0.example.com"
  }

  expect_failures = [var.name]
}
