// Allegiance VPC Endpoint DNS entries per stage
// Obtained from https://allegiance.corp.amazon.com after registering VPC Endpoint Service
// To look up later: https://allegiance.corp.amazon.com/search_private_links_from_maws

// Known Allegiance Hosted Zone IDs per region (these are static, owned by Allegiance transit accounts)
export const ALLEGIANCE_HOSTED_ZONE_IDS: Record<string, string> = {
  'us-west-2': 'Z1YSA3EXCYUU9Z',
  'us-east-1': 'Z7HUB22UULQXV',
  'eu-west-1': 'Z38GZ743OKFT7T',
};

// Allegiance VPC Endpoint DNS entries per stage/region
// Fill in after registering each stage with Allegiance
export const ALLEGIANCE_DNS: Record<string, Record<string, string>> = {
  alpha: {
    'us-west-2': 'vpce-083bde505eb298d21-69dq3dgg.vpce-svc-03b6b9e8cf48a2e87.us-west-2.vpce.amazonaws.com',
  },
  // beta: {
  //   'us-west-2': '<fill after Allegiance registration>',
  // },
  // gamma: {
  //   'us-east-1': '<fill after Allegiance registration>',
  // },
};
