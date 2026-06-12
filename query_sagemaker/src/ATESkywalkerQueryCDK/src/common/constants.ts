export const APPLICATION_ACCOUNT_ID = '923561819404';
export const ALPHA_ACCOUNT_ID = '465556393784';
export const BETA_ACCOUNT_ID = '278522729570';
export const GAMMA_ACCOUNT_ID = '817294254658';

// ATESkywalkerIngest account IDs (where OpenSearch collections live)
export const INGEST_ALPHA_ACCOUNT_ID = '948580600005';
export const INGEST_BETA_ACCOUNT_ID = '334296258454';
export const BINDLE_GUID = 'amzn1.bindle.resource.7pk5lr35qx4l5f5ks47a';
export const PIPELINE_ID = '9441834';
export const PIPELINE_NAME = 'ATESkywalkerQuery';
export const SERVICE_NAME = 'ATESkywalkerQuery';
export const TEAM_EMAIL = 'nmatnich@amazon.com';
export const VERSION_SET = 'ATESkywalkerQuery/development';

// DNS delegation — root hosted zone is managed by the DNS pipeline (ATESkywalkerQueryDNS).
// The hosted zone name is in the format <service-domain>.<org-domain>.<supernova's base domain>.
// To update to a real domain, change ORG_DOMAIN_NAME and SERVICE_DOMAIN_NAME in the DNS pipeline's
// constants.ts, then update HOSTED_ZONE_NAME here to match.
// To search for existing org domains, visit: https://supernova.amazon.dev/index.html
export const HOSTED_ZONE_NAME = 'skywalker.gref.amazon.dev';
export const DNS_DELEGATION_ACCOUNT = '703384432136';

export const DELEGATION_ROLE_PREFIX = 'dns-auto-update-role';
