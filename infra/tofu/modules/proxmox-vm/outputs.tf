output "vm_id" {
  description = "VMID."
  value       = proxmox_virtual_environment_vm.this.vm_id
}

output "name" {
  description = "VM name."
  value       = proxmox_virtual_environment_vm.this.name
}

output "ipv4_addresses" {
  description = "IPv4 addresses per interface, as reported by the guest agent. Empty until the agent answers, which on first boot waits for cloud-init's package install."
  value       = proxmox_virtual_environment_vm.this.ipv4_addresses
}

output "mac_addresses" {
  description = "MAC addresses per interface."
  value       = proxmox_virtual_environment_vm.this.mac_addresses
}
