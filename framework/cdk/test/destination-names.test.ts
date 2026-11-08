import {
  destinationBucketName,
  destinationBucketNameJsonata,
  destinationRoleSuffix,
} from "../lib/common/destination-names";

describe("destinationBucketName", () => {
  it("is ccf-<destination ID>-<account>-<region>", () => {
    const name = destinationBucketName({
      destinationId: "dst-7c41e9a2b5d0",
      account: "111122223333",
      region: "us-east-1",
    });

    expect(name).toBe("ccf-dst-7c41e9a2b5d0-111122223333-us-east-1");
  });

  it("has a JSONata form that joins the same parts", () => {
    const expression = destinationBucketNameJsonata({
      destinationId: "$destinationId",
      account: "$account",
      region: "$region",
    });

    expect(expression).toBe("{% 'ccf-' & $destinationId & '-' & $account & '-' & $region %}");
  });
});

describe("destinationRoleSuffix", () => {
  it("keeps a 45-character name whole", () => {
    expect(destinationRoleSuffix("a".repeat(45))).toBe("a".repeat(45));
  });

  it("shortens a longer name to 45 characters ending in 8 hex of its hash, the same every time", () => {
    const suffix = destinationRoleSuffix("a".repeat(64));

    expect(suffix).toHaveLength(45);
    expect(suffix).toMatch(/^a{36}-[0-9a-f]{8}$/);
    expect(destinationRoleSuffix("a".repeat(64))).toBe(suffix);
  });

  it.each(["", "has space", "a".repeat(65), "slash/name"])(
    "rejects the destination name %j",
    (name) => {
      expect(() => destinationRoleSuffix(name)).toThrow(/destination name/);
    },
  );
});
