import { Template } from 'aws-cdk-lib/assertions';
import { DeploymentEnvironmentFactory } from '@amzn/pipelines';
import { App } from 'aws-cdk-lib';
import { OpenSearchStack } from '../lib/openSearchStack';

test('creates fragment index mapping with faiss vector engine', () => {
  const app = new App();
  const stack = new OpenSearchStack(app, 'OpenSearchTest', {
    stage: 'alpha',
    isProd: false,
    env: DeploymentEnvironmentFactory.fromAccountAndRegion('test-account', 'us-west-2', 'unique-id'),
  });
  const template = Template.fromStack(stack).toJSON();
  const resources = Object.values(template.Resources) as Array<{ Properties?: Record<string, unknown> }>;
  const indexResource = resources.find((resource) => resource.Properties?.indexBody);
  if (!indexResource?.Properties?.indexBody) {
    throw new Error('Expected custom resource with indexBody');
  }

  const indexBody = JSON.parse(indexResource.Properties.indexBody as string);
  const properties = indexBody.mappings.properties;

  expect(properties.embedding.dimension).toBe(1024);
  expect(properties.embedding.method.engine).toBe('faiss');
  expect(properties.fragment_id.type).toBe('keyword');
  expect(properties.source_id.type).toBe('keyword');
  expect(properties.text.type).toBe('text');
  expect(properties.followup_fragment_ids.type).toBe('keyword');
  expect(properties.followup_fragment_ids.index).toBe(false);
  // Retired vocabulary must not reappear in the mapping.
  expect(properties.title).toBeUndefined();
  expect(properties.child_fragment_ids).toBeUndefined();
  expect(properties.chunk_id).toBeUndefined();
  expect(properties.content).toBeUndefined();
});
