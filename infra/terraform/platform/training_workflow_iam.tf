# Main-only cloud training identity; independent from the release role.
resource "aws_iam_role" "github_training" {
  name               = "${local.cluster_name}-github-training"
  assume_role_policy = data.aws_iam_policy_document.m32_github_trust.json
}

data "aws_iam_policy_document" "github_training" {
  statement {
    sid       = "ManageTemporaryEksApiCidr"
    actions   = ["eks:DescribeCluster", "eks:UpdateClusterConfig", "eks:DescribeUpdate", "eks:ListUpdates"]
    resources = ["arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${local.cluster_name}"]
  }
  statement {
    sid       = "GetEcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "PushAndInspectTrainingImage"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart", "ecr:PutImage", "ecr:DescribeImages", "ecr:BatchGetImage"
    ]
    resources = ["arn:aws:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/mlops-aws-training-${var.environment}"]
  }
  statement {
    sid       = "ListVerificationArtifacts"
    actions   = ["s3:ListBucket"]
    resources = [local.m32_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["mlflow/*"]
    }
  }
  statement {
    sid       = "ReadVerificationArtifacts"
    actions   = ["s3:GetObject"]
    resources = ["${local.m32_bucket_arn}/mlflow/*"]
  }
}

resource "aws_iam_role_policy" "github_training" {
  name   = "training-ecr-push-artifact-verification"
  role   = aws_iam_role.github_training.id
  policy = data.aws_iam_policy_document.github_training.json
}
