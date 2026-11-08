/**
 * The bucket policy the destination workflow writes once on every destination bucket. It holds one
 * statement that denies requests without TLS and grants nothing: every principal that reads or
 * writes the bucket is a tenant-account role, and same-account identity policies are enough.
 */

/** The bucket's ARN and its objects' ARN: literals, or whole JSONata expressions in a workflow. */
export interface DestinationBucketPolicyRequest {
  readonly bucketArn: string;
  readonly objectsArn: string;
}

/** The destination bucket policy for one bucket. */
export const destinationBucketPolicy = (request: DestinationBucketPolicyRequest) => ({
  Version: "2012-10-17",
  Statement: [
    {
      Sid: "DenyInsecureTransport",
      Effect: "Deny",
      Principal: "*",
      Action: "s3:*",
      Resource: [request.bucketArn, request.objectsArn],
      Condition: { Bool: { "aws:SecureTransport": "false" } },
    },
  ],
});
