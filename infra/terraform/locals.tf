locals {
  resource_suffix = substr(sha256(var.session_id), 0, 8)
  name_prefix     = "trackrelay-${local.resource_suffix}"

  common_tags = {
    Environment = "experiment"
    ManagedBy   = "terraform"
    Project     = "TrackRelay"
    SessionId   = var.session_id
  }
}
