import {
  apiPathPrefix,
  enumMember,
  enumValues,
  jobContainerContract,
  maxLength,
  metricsContract,
  numberRange,
  operationRoutes,
  runtimeRoleTagsContract,
  runtimeRoutes,
  settingsContract,
  stringPattern,
  summaryAttributes,
  tablesContract,
} from "../lib/common/contracts";

describe("the generated contract readers", () => {
  it("lists the runtime-callable routes sorted by path and method", () => {
    const routes = runtimeRoutes();

    expect(routes).toEqual(
      [...routes].sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method)),
    );
    expect(routes).toContainEqual({
      method: "POST",
      path: "/api/v1/destinations/{destination_id}/custom-connectors/{connector_id}/documents/batch-put",
    });
    expect(routes).not.toContainEqual({ method: "POST", path: "/api/v1/destinations" });
  });

  it("names every path's shared prefix", () => {
    expect(apiPathPrefix()).toBe("/api/v1/destinations");
  });

  it("reads an enum's values", () => {
    expect(enumValues("JobStatus")).toEqual([
      "STARTED",
      "RUNNING",
      "STOPPING",
      "STOPPED",
      "FAILED",
      "SUCCEEDED",
    ]);
  });

  it("turns named operations into execute-api routes with each label as *", () => {
    expect(operationRoutes(["CreateDestination", "GetDestination", "DeleteDestination"])).toEqual([
      { method: "POST", path: "/api/v1/destinations" },
      { method: "GET", path: "/api/v1/destinations/*" },
      { method: "DELETE", path: "/api/v1/destinations/*" },
    ]);
  });

  it("fails naming an operation the model doesn't have", () => {
    expect(() => operationRoutes(["RenameDestination"])).toThrow("RenameDestination");
  });

  it("fails naming the schema and the member when an enum member is missing", () => {
    expect(() => enumMember({ schema: "DestinationType", member: "MISSING" })).toThrow(
      /DestinationType has no member MISSING/,
    );
    expect(enumMember({ schema: "DestinationType", member: "BEDROCK" })).toBe("BEDROCK");
  });

  it("reads patterns, lengths, and ranges", () => {
    expect(stringPattern("DestinationId").test("dst-7c41e9a2b5d0")).toBe(true);
    expect(maxLength({ schema: "Destination", member: "status_reason" })).toBe(2048);
    expect(numberRange({ schema: "ContainerProperties", member: "timeout" })).toEqual({
      minimum: 60,
      maximum: 604800,
    });
  });

  it("projects a summary schema's members that aren't keys", () => {
    expect(
      summaryAttributes({
        schema: "JobSummary",
        keys: ["tenant_arn_prefix", "job_id", "connector_key", "created_at"],
      }),
    ).toEqual(["arn", "status", "updated_at"]);
  });

  it("reads the tables, metrics, and settings contracts", () => {
    expect(tablesContract().destinations.name).toBe("CcfDestinations");
    expect(metricsContract().namespace).toBe("CustomConnectorFramework");
    expect(settingsContract().handlers.api_handler).toContain("TABLES__DESTINATIONS");
  });

  it("reads the five runtime role tag keys", () => {
    expect(runtimeRoleTagsContract()).toEqual({
      destinationId: "ccf:destination-id",
      connectorId: "ccf:connector-id",
      tenantAccount: "ccf:tenant-account",
      sourceAccessRole: "ccf:source-access-role",
      logDeliveryRole: "ccf:log-delivery-role",
    });
  });

  it("reads the job container's names, variables, and exit codes", () => {
    const contract = jobContainerContract();
    expect(contract.containers).toEqual({ connector: "connector", logRouter: "log-router" });
    expect(contract.variables.jobId).toBe("CUSTOM_CONNECTOR_FRAMEWORK_JOB_ID");
    expect(contract.exitCodes.FRAMEWORK_API_UNAVAILABLE).toBe(76);
  });
});
