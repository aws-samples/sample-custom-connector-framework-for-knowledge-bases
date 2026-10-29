export interface ClientImageAssetsRepositoryNameRequest {
  readonly account: string;
  readonly region: string;
}
/** Build the dedicated client-owned repository name used for connector image assets. */
export const createClientImageAssetsRepositoryName = ({
  account,
  region,
}: ClientImageAssetsRepositoryNameRequest): string =>
  `ccf-client-image-assets-${account}-${region}`;
