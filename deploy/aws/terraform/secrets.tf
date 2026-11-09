locals {
  database_url = "postgresql://agenticml@${aws_db_instance.main.address}:5432/agenticml?sslmode=verify-full&sslrootcert=/etc/agenticml/rds-ca.pem"
  environment = {
    AGENTICML_SERVER_DATABASE_URL     = local.database_url
    AGENTICML_SERVER_OBJECTS          = "s3://${aws_s3_bucket.objects.id}/hosted"
    AGENTICML_SERVER_PUBLIC_URL       = var.public_url
    AGENTICML_SERVER_FIREBASE_PROJECT = var.firebase_project
    AGENTICML_SERVER_FIREBASE_API_KEY = var.firebase_api_key
    AGENTICML_SERVER_OPEN_SIGNUP      = tostring(var.open_signup)
    AGENTICML_SERVER_LIMITS           = "/etc/agenticml/limits.json"
    AGENTICML_SERVER_TRUSTED_PROXY    = join(",", aws_subnet.public[*].cidr_block)
    AWS_REGION                        = var.region
  }
}
