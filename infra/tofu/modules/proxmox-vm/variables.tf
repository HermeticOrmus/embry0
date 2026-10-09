variable "node_name" {
  description = "Proxmox node the VM runs on."
  type        = string

  validation {
    condition     = length(trimspace(var.node_name)) > 0
    error_message = "node_name must not be empty."
  }
}

variable "vm_id" {
  description = "VMID. Must be free on the cluster: `qm config <vmid>` should report that the file does not exist."
  type        = number

  validation {
    condition     = var.vm_id >= 100 && var.vm_id <= 999999999 && floor(var.vm_id) == var.vm_id
    error_message = "vm_id must be an integer from 100 to 999999999."
  }
}

variable "name" {
  description = "VM name, a single DNS label."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$", var.name))
    error_message = "name must be a single DNS label: letters, digits and inner hyphens, at most 63 characters."
  }
}

variable "cores" {
  description = "vCPU cores, on one socket."
  type        = number

  validation {
    condition     = var.cores >= 1 && floor(var.cores) == var.cores
    error_message = "cores must be a positive integer."
  }
}

variable "memory_mb" {
  description = "Maximum memory in MiB (qm --memory)."
  type        = number

  validation {
    condition     = var.memory_mb >= 1024 && floor(var.memory_mb) == var.memory_mb
    error_message = "memory_mb must be an integer of at least 1024."
  }
}

variable "balloon_min_mb" {
  description = "Balloon floor in MiB (qm --balloon): the memory the host guarantees. Must not exceed memory_mb."
  type        = number

  validation {
    condition     = var.balloon_min_mb >= 0 && floor(var.balloon_min_mb) == var.balloon_min_mb
    error_message = "balloon_min_mb must be a non-negative integer."
  }
}

variable "disk_size_gb" {
  description = "Boot disk size in GiB. The cloud image is imported, then grown to this size."
  type        = number

  validation {
    condition     = var.disk_size_gb >= 10 && floor(var.disk_size_gb) == var.disk_size_gb
    error_message = "disk_size_gb must be an integer of at least 10."
  }
}

variable "datastore_id" {
  description = "Storage for the boot disk and the cloud-init drive."
  type        = string

  validation {
    condition     = length(trimspace(var.datastore_id)) > 0
    error_message = "datastore_id must not be empty."
  }
}

variable "disk_image_file_id" {
  description = "The cloud image to import, as a volume of content type `import`: `<storage>:import/<file>.qcow2` (or .raw, .vmdk). The import goes through the Proxmox API, so no SSH is needed."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]*:import/[^/]+\\.(qcow2|raw|vmdk)$", var.disk_image_file_id))
    error_message = "disk_image_file_id must look like <storage>:import/<file>.qcow2 (or .raw, .vmdk)."
  }
}

variable "user_data_file_id" {
  description = "The cloud-init user data snippet, `<storage>:snippets/<file>`: a copy of infra/cloud-init/embry0-user.yaml.example with your values."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9_.-]*:snippets/[^/]+$", var.user_data_file_id))
    error_message = "user_data_file_id must look like <storage>:snippets/<file>."
  }
}

variable "network_bridge" {
  description = "Bridge for the VM's one virtio NIC."
  type        = string

  validation {
    condition     = length(trimspace(var.network_bridge)) > 0
    error_message = "network_bridge must not be empty."
  }
}

variable "ipv4_address" {
  description = "\"dhcp\", or a static address in CIDR notation."
  type        = string
  default     = "dhcp"

  validation {
    condition     = var.ipv4_address == "dhcp" || can(cidrhost(var.ipv4_address, 0))
    error_message = "ipv4_address must be \"dhcp\" or an address in CIDR notation."
  }
}

variable "ipv4_gateway" {
  description = "IPv4 gateway for a static address. Null with dhcp."
  type        = string
  default     = null
}

variable "tags" {
  description = "Proxmox tags. Metadata only."
  type        = list(string)
  default     = []
}
