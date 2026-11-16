variable "aws_profile" {
  description = "AWS profile used for this lab."
  type        = string
  default     = "mlops-dev"
}

variable "aws_region" {
  description = "AWS region for the platform."
  type        = string
  default     = "ap-southeast-1"
}

variable "environment" {
  description = "Deployment environment."
  type        = string
  default     = "dev"
}

variable "eks_public_access_cidrs" {
  description = "Explicit administrator IPv4 addresses allowed to reach the public Kubernetes API, as /32 CIDRs."
  type        = list(string)

  validation {
    condition = length(var.eks_public_access_cidrs) > 0 && alltrue([
      for cidr in var.eks_public_access_cidrs :
      can(cidrnetmask(cidr)) && try(tonumber(split("/", cidr)[1]), 0) == 32
    ])
    error_message = "Provide at least one administrator IPv4 address as a /32 CIDR; broad ranges are not allowed."
  }
}

variable "github_oidc_subject" {
  description = "Exact GitHub main-branch OIDC subject; match the repository's subject customization."
  type        = string
  default     = "repo:HaiXon231@204581707/mlops-platform-aws@1424395869:ref:refs/heads/main"
  validation {
    condition     = startswith(var.github_oidc_subject, "repo:") && endswith(var.github_oidc_subject, ":ref:refs/heads/main")
    error_message = "The OIDC subject must identify a repository and refs/heads/main."
  }
}
