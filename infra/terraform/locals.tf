locals {
  common_tags = {
    Environment = "experiment"
    ManagedBy   = "terraform"
    Project     = "TrackRelay"
    SessionId   = var.session_id
  }
}
