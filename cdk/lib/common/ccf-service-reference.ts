/** Exact public deployment coordinates required by a CCF client stack. */
export interface CcfServiceReference {
  readonly endpoint: string;
  readonly region: string;
  readonly serviceAccountId: string;
  readonly frameworkExecutionPrincipalArn: string;
  readonly restApiId: string;
}
