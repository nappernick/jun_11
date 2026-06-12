import { Client } from '@opensearch-project/opensearch';
import { AwsSigv4Signer } from '@opensearch-project/opensearch/aws';
import { defaultProvider } from '@aws-sdk/credential-provider-node';

const CLIENT_TIMEOUT_MS = 30000;
const MAX_RETRIES = 20;
const RETRY_DELAY_MS = 10000;

interface CustomResourceEvent {
  RequestType: 'Create' | 'Update' | 'Delete';
  ResourceProperties: {
    indexName: string;
    collectionEndpoint: string;
    indexBody: string;
  };
}

interface CustomResourceResponse {
  PhysicalResourceId: string;
}

interface OpenSearchError {
  meta?: { statusCode?: number };
  body?: { error?: { type?: string } };
}

function isOpenSearchError(error: unknown): error is OpenSearchError {
  return typeof error === 'object' && error !== null;
}

export async function onEvent(event: CustomResourceEvent): Promise<CustomResourceResponse> {
  console.log('OnEvent called:', JSON.stringify(event));

  const indexName = event.ResourceProperties.indexName;
  const collectionEndpoint = event.ResourceProperties.collectionEndpoint;
  const indexBody = JSON.parse(event.ResourceProperties.indexBody);

  const region = process.env.AWS_REGION;
  if (!region) {
    throw new Error('AWS_REGION environment variable is not set');
  }

  const client = new Client({
    ...AwsSigv4Signer({
      region,
      service: 'aoss',
      getCredentials: defaultProvider(),
    }),
    maxRetries: 5,
    node: collectionEndpoint,
    requestTimeout: CLIENT_TIMEOUT_MS,
  });

  if (event.RequestType === 'Delete') {
    console.log('Delete requested — retaining index');
    return { PhysicalResourceId: indexName };
  }

  for (let attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    try {
      const exists = await client.indices.exists({ index: indexName });
      if (exists.body) {
        console.log(`Index ${indexName} already exists`);
        return { PhysicalResourceId: indexName };
      }

      console.log(`Creating index ${indexName} (attempt ${attempt}/${MAX_RETRIES})`);
      await client.indices.create({ index: indexName, body: indexBody });
      console.log('Index created successfully');
      return { PhysicalResourceId: indexName };
    } catch (error: unknown) {
      if (!isOpenSearchError(error)) {
        throw error;
      }
      const status = error.meta?.statusCode;
      if ((status === 401 || status === 403) && attempt < MAX_RETRIES) {
        console.log(`${status} — policy not propagated, retrying in ${RETRY_DELAY_MS}ms...`);
        await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
      } else if (error.body?.error?.type === 'resource_already_exists_exception') {
        console.log('Index already exists');
        return { PhysicalResourceId: indexName };
      } else {
        throw error;
      }
    }
  }

  throw new Error(`Failed to create index after ${MAX_RETRIES} attempts`);
}
