---
description: The IAM roles a Custom Connector Framework tenant owns, who assumes each one, and their full trust and permissions policies.
---

# Roles in your account

The framework doesn't hold a credential for your account. It acts there only through roles your stacks create, and each role trusts a named framework role only when the call carries an external ID. The CDK constructs that deploy your destination and connector create every role for you. This page shows what each one allows, so you can review it.

| Role | Created by | Assumed by | What it may do |
| --- | --- | --- | --- |
| Destination role, `CcfDestinationRole-<name>`, one per destination | The destination stack | The framework's API, its destination workflows, its connector deletion workflow, and its ingestion consumer | Create and delete the destination's bucket, knowledge base, data source, knowledge base role, and ingestion log delivery. Delete documents' files, set metadata fields, and start ingestion. A job never uses it. |
| Source-access role, `CcfSourceRole-<connector name>`, one per custom connector | `CustomConnector` | The custom connector's runtime role, in its job | Read the custom connector's secret. Write, read, list, and delete documents under the custom connector's prefix in the destination bucket. Anything else you grant it. |
| Log-delivery role, `CcfLogDeliveryRole-<connector name>`, optional | `CustomConnector`, when log delivery is on | The job's log router, as the runtime role | Write to the custom connector's log group |
| Knowledge base role, `CcfKnowledgeBase-<destination ID>` | The destination workflow, through the destination role | [Amazon Bedrock](https://aws.amazon.com/bedrock/) | Read destination buckets and publish the knowledge base's metrics |

An external ID stops a third party from tricking the framework into assuming your role on its behalf, which is called the [confused deputy problem](https://docs.aws.amazon.com/IAM/latest/UserGuide/confused-deputy.html). The framework generates one external ID per custom connector, which both of its roles require. A destination role requires a state machine's ARN as its external ID.

In the policies below, `<p>` is the partition, `<F>` the service account, `<T>` your tenant account, `<r>` the service Region, and `<name>` the destination's name.

## Destination role

`DestinationRole` creates `role/ccf/destinations/CcfDestinationRole-<name>`, shortened with a hash when the name makes it longer than 64 characters. Its trust policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfApiRole" },
      "Condition": { "StringEquals": {
        "sts:ExternalId": ["arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationWorkflow"] } } },
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfIngestionConsumerRole" },
      "Condition": { "StringEquals": {
        "sts:ExternalId": ["arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationWorkflow"] } } },
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfDestinationWorkflowRole" },
      "Condition": { "StringEquals": { "sts:ExternalId": [
        "arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationWorkflow",
        "arn:<p>:states:<r>:<F>:stateMachine:CcfDestinationDeletionWorkflow"] } } },
    { "Effect": "Allow", "Action": "sts:AssumeRole",
      "Principal": { "AWS": "arn:<p>:iam::<F>:role/ccf/CcfConnectorDeletionWorkflowRole" },
      "Condition": { "StringEquals": {
        "sts:ExternalId": ["arn:<p>:states:<r>:<F>:stateMachine:CcfConnectorDeletionWorkflow"] } } }
  ]
}
```

When an [AWS Step Functions](https://aws.amazon.com/step-functions/) task assumes a role, it sends the state machine's ARN as the external ID, and has no field for another value. So each state machine's ARN is the external ID. The API and the ingestion consumer call [AWS STS](https://docs.aws.amazon.com/STS/latest/APIReference/welcome.html) themselves, and send the `CcfDestinationWorkflow` ARN.

Its inline policy, `CcfDestinationPermissions`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "DestinationBuckets", "Effect": "Allow",
      "Action": ["s3:CreateBucket", "s3:DeleteBucket", "s3:PutBucketPolicy", "s3:PutEncryptionConfiguration",
                 "s3:PutBucketPublicAccessBlock", "s3:PutBucketTagging", "s3:ListBucket"],
      "Resource": "arn:<p>:s3:::ccf-dst-*" },
    { "Sid": "DestinationObjects", "Effect": "Allow",
      "Action": ["s3:DeleteObject", "s3:GetObject"], "Resource": "arn:<p>:s3:::ccf-dst-*/*" },
    { "Sid": "GlobalAccessControlList", "Effect": "Allow",
      "Action": "s3:PutObject", "Resource": "arn:<p>:s3:::ccf-dst-*/ccf-acl/*" },
    { "Sid": "CreateTaggedKnowledgeBase", "Effect": "Allow",
      "Action": "bedrock:CreateKnowledgeBase", "Resource": "*",
      "Condition": { "Null": { "aws:RequestTag/ccf:destination-id": "false" },
                     "ForAllValues:StringEquals": { "aws:TagKeys": ["ccf:destination-id"] } } },
    { "Sid": "TagTaggedKnowledgeBase", "Effect": "Allow",
      "Action": "bedrock:TagResource", "Resource": "arn:<p>:bedrock:<r>:<T>:knowledge-base/*",
      "Condition": { "Null": { "aws:RequestTag/ccf:destination-id": "false" },
                     "ForAllValues:StringEquals": { "aws:TagKeys": ["ccf:destination-id"] } } },
    { "Sid": "ManageTaggedKnowledgeBase", "Effect": "Allow",
      "Action": ["bedrock:CreateDataSource", "bedrock:UpdateKnowledgeBase", "bedrock:StartIngestionJob",
                 "bedrock:StopIngestionJob", "bedrock:AllowVendedLogDeliveryForResource"],
      "Resource": "arn:<p>:bedrock:<r>:<T>:knowledge-base/*",
      "Condition": { "Null": { "aws:ResourceTag/ccf:destination-id": "false" } } },
    { "Sid": "IngestionLogGroups", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:TagResource", "logs:PutRetentionPolicy",
                 "logs:PutResourcePolicy", "logs:DeleteLogGroup"],
      "Resource": ["arn:<p>:logs:<r>:<T>:log-group:/aws/vendedlogs/bedrock/knowledge-base/ccf-*",
                   "arn:<p>:logs:<r>:<T>:log-group:/aws/vendedlogs/bedrock/knowledge-base/ccf-*:*"] },
    { "Sid": "IngestionLogDeliveries", "Effect": "Allow",
      "Action": ["logs:PutDeliverySource", "logs:GetDeliverySource", "logs:DeleteDeliverySource",
                 "logs:PutDeliveryDestination", "logs:GetDeliveryDestination", "logs:DeleteDeliveryDestination",
                 "logs:CreateDelivery", "logs:GetDelivery", "logs:DeleteDelivery"],
      "Resource": ["arn:<p>:logs:<r>:<T>:delivery-source:ccf-*",
                   "arn:<p>:logs:<r>:<T>:delivery-destination:ccf-*",
                   "arn:<p>:logs:<r>:<T>:delivery:*"] },
    { "Sid": "DescribeIngestionLogDelivery", "Effect": "Allow",
      "Action": ["logs:DescribeDeliveries", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"],
      "Resource": "*" },
    { "Sid": "ReadAndDeleteKnowledgeBases", "Effect": "Allow",
      "Action": ["bedrock:GetKnowledgeBase", "bedrock:DeleteKnowledgeBase", "bedrock:GetDataSource",
                 "bedrock:DeleteDataSource", "bedrock:GetIngestionJob", "bedrock:ListIngestionJobs"],
      "Resource": "arn:<p>:bedrock:<r>:<T>:knowledge-base/*" },
    { "Sid": "CreateBoundedKnowledgeBaseRole", "Effect": "Allow",
      "Action": "iam:CreateRole", "Resource": "arn:<p>:iam::<T>:role/ccf/destinations/CcfKnowledgeBase-*",
      "Condition": { "StringEquals": {
        "iam:PermissionsBoundary": "arn:<p>:iam::<T>:policy/ccf/destinations/CcfDestinationBoundary-<name>" } } },
    { "Sid": "ManageKnowledgeBaseRole", "Effect": "Allow",
      "Action": ["iam:TagRole", "iam:DeleteRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:GetRole"],
      "Resource": "arn:<p>:iam::<T>:role/ccf/destinations/CcfKnowledgeBase-*" },
    { "Sid": "PassKnowledgeBaseRole", "Effect": "Allow",
      "Action": "iam:PassRole", "Resource": "arn:<p>:iam::<T>:role/ccf/destinations/CcfKnowledgeBase-*",
      "Condition": { "StringEquals": { "iam:PassedToService": "bedrock.amazonaws.com" } } }
  ]
}
```

- `CreateKnowledgeBase` has no resource type, so its statement names `*` and relies on the tag conditions.
- `UpdateKnowledgeBase` sets a knowledge base's metadata fields and passes its role again, which `PassKnowledgeBaseRole` allows.
- The read and delete statement has no tag condition, because a knowledge base that's being deleted no longer resolves its tags.
- The log delivery statements name the destination's log group, delivery source, and delivery destination by their `ccf-` prefix, and any delivery, because [Amazon CloudWatch Logs](https://aws.amazon.com/cloudwatch/) gives each delivery a random ID. The describe actions take no resource, and `CreateDelivery` calls them to write the log group's resource policy.

The role's own permissions boundary, `CcfDestinationRoleBoundary-<name>`, holds the same statements. The `CcfDestinationBoundary-<name>` managed policy, the boundary of every knowledge base role the workflow creates, allows only `s3:GetObject*`, `s3:GetBucket*`, and `s3:List*` on `ccf-dst-*` buckets and their objects, and `cloudwatch:PutMetricData` in the `AWS/Bedrock/KnowledgeBases` namespace.

The destination roles in one tenant account can each manage any knowledge base that carries the `ccf:destination-id` tag in that account. Keep destinations that need separate administrators in separate accounts.

## Source-access role

`CustomConnector` creates `CcfSourceRole-<connector name>`. When the custom connector's name makes the role name longer than 64 characters, the construct cuts the name to fit and adds a hash. Its trust policy, with `<RT>` the custom connector's runtime role ARN and `<X>` its external ID, which `CreateCustomConnector` and `GetCustomConnector` return:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:<p>:iam::<F>:root" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": { "sts:ExternalId": "<X>" },
      "ArnEquals": { "aws:PrincipalArn": "<RT>" }
    }
  }]
}
```

The trust names the service account and pins the runtime role with `aws:PrincipalArn`, rather than naming the runtime role as its principal. The API creates the runtime role seconds before this one, and [IAM](https://aws.amazon.com/iam/) rejects a role principal it can't resolve yet.

Its inline policy, `CcfConnectorAccess`, with `<B>` the destination bucket and `<C>` the custom connector ID:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ConnectorPrefixObjects", "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:<p>:s3:::<B>/connector_id=<C>/*" },
    { "Sid": "ConnectorPrefixList", "Effect": "Allow",
      "Action": "s3:ListBucket", "Resource": "arn:<p>:s3:::<B>",
      "Condition": { "StringLike": { "s3:prefix": "connector_id=<C>/*" } } }
  ]
}
```

Grant the rest on the construct itself: the custom connector is the role's grant principal, so `secret.grantRead(connector)` lets the job read its secret, as the example GitHub stack does. Both of the custom connector's roles have the permissions boundary `CcfConnectorBoundary-<connector name>`. It allows only [Amazon S3](https://aws.amazon.com/s3/), [AWS Secrets Manager](https://aws.amazon.com/secrets-manager/), [AWS KMS](https://aws.amazon.com/kms/), Amazon CloudWatch Logs, and the services in `sourceAccessServices`, so a grant to any other service has no effect.

## Log-delivery role

With log delivery on, `CustomConnector` also creates the log group `/ccf/connectors/<connector name>` and `CcfLogDeliveryRole-<connector name>`, with the same trust policy as the source-access role and this inline policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "WriteLogDestination", "Effect": "Allow",
      "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:<p>:logs:<r>:<T>:log-group:/ccf/connectors/<connector name>:*" }
  ]
}
```

The custom connector's `log_destination` names the log group and the role. The job's log router assumes the role and writes each line the container prints to a stream named after the job ID.
