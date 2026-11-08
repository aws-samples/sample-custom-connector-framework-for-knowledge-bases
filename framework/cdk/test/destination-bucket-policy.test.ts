import { destinationBucketPolicy } from "../lib/common/destination-bucket-policy";

const BUCKET = "ccf-dst-7c41e9a2b5d0-111122223333-us-east-1";

describe("destinationBucketPolicy", () => {
  test("is the document: one DenyInsecureTransport statement on the bucket and its objects", () => {
    const policy = destinationBucketPolicy({
      bucketArn: `arn:aws:s3:::${BUCKET}`,
      objectsArn: `arn:aws:s3:::${BUCKET}/*`,
    });

    expect(policy).toEqual({
      Version: "2012-10-17",
      Statement: [
        {
          Sid: "DenyInsecureTransport",
          Effect: "Deny",
          Principal: "*",
          Action: "s3:*",
          Resource: [`arn:aws:s3:::${BUCKET}`, `arn:aws:s3:::${BUCKET}/*`],
          Condition: { Bool: { "aws:SecureTransport": "false" } },
        },
      ],
    });
  });

  test("names the ARNs it is given, in any partition", () => {
    const policy = destinationBucketPolicy({
      bucketArn: `arn:aws-us-gov:s3:::${BUCKET}`,
      objectsArn: `arn:aws-us-gov:s3:::${BUCKET}/*`,
    });

    expect(policy.Statement[0].Resource).toEqual([
      `arn:aws-us-gov:s3:::${BUCKET}`,
      `arn:aws-us-gov:s3:::${BUCKET}/*`,
    ]);
  });
});
