import { CfnOutput, CustomResource, Duration, RemovalPolicy, Stack } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { DeploymentEnvironment, DeploymentStack, SoftwareType } from '@amzn/pipelines';
import { CfnAccessPolicy, CfnCollection, CfnSecurityPolicy } from 'aws-cdk-lib/aws-opensearchserverless';
import {
  AccountPrincipal,
  Effect,
  ManagedPolicy,
  PolicyDocument,
  PolicyStatement,
  Role,
  ServicePrincipal,
} from 'aws-cdk-lib/aws-iam';
import { Provider } from 'aws-cdk-lib/custom-resources';
import { NodejsFunction } from 'aws-cdk-lib/aws-lambda-nodejs';
import { Runtime } from 'aws-cdk-lib/aws-lambda';
import { resolve } from 'path';

export interface OpenSearchStackProps {
  readonly env: DeploymentEnvironment;
  readonly stage: string;
  readonly isProd: boolean;
  /** Query service account IDs that need read-only access via assume-role */
  readonly queryServiceAccountIds?: string[];
}

export class OpenSearchStack extends DeploymentStack {
  public readonly collectionEndpoint: string;
  public readonly collectionArn: string;

  constructor(scope: Construct, id: string, props: OpenSearchStackProps) {
    super(scope, id, { env: props.env, softwareType: SoftwareType.INFRASTRUCTURE });

    const collectionName = `skywalker-faq-${props.stage}`;

    // Encryption policy — AWS-owned key (must exist before collection)
    const encryptionPolicy = new CfnSecurityPolicy(this, 'EncryptionPolicy', {
      name: `${collectionName}-enc`,
      type: 'encryption',
      policy: JSON.stringify({
        Rules: [{ ResourceType: 'collection', Resource: [`collection/${collectionName}`] }],
        AWSOwnedKey: true,
      }),
    });

    // Network policy — public endpoint with IAM auth (standard practice for internal AOSS collections)
    const networkPolicy = new CfnSecurityPolicy(this, 'NetworkPolicy', {
      name: `${collectionName}-net`,
      type: 'network',
      policy: JSON.stringify([
        {
          Description: 'IAM-authenticated access (no VPC endpoint required)',
          Rules: [
            { ResourceType: 'collection', Resource: [`collection/${collectionName}`] },
            { ResourceType: 'dashboard', Resource: [`collection/${collectionName}`] },
          ],
          AllowFromPublic: true,
        },
      ]),
    });

    // Collection
    const collection = new CfnCollection(this, 'FaqEvidenceCollection', {
      name: collectionName,
      type: 'VECTORSEARCH',
      description: 'Skywalker FAQ evidence vector store',
      standbyReplicas: props.isProd ? 'ENABLED' : 'DISABLED',
    });
    collection.addDependency(encryptionPolicy);
    collection.addDependency(networkPolicy);
    if (props.isProd) {
      collection.applyRemovalPolicy(RemovalPolicy.RETAIN);
    }

    // Data access policy — scoped to account root; IAM policies on individual roles gate actual access
    const accessPolicy = new CfnAccessPolicy(this, 'DataAccessPolicy', {
      name: `${collectionName}-access`,
      type: 'data',
      policy: JSON.stringify([
        {
          Description: 'Access for IAM principals in this account with aoss:APIAccessAll',
          Rules: [
            {
              ResourceType: 'collection',
              Resource: [`collection/${collectionName}`],
              // DescribeCollectionItems for read; CreateCollectionItems/UpdateCollectionItems
              // are the collection-level write permissions that govern cluster objects such
              // as the _search/pipeline used for hybrid search (R8). Without these, creating
              // the skywalker-faq-hybrid pipeline returns 403. AOSS Serverless supports
              // search pipelines (GA Aug 2025); this is the IAM grant that enables it.
              Permission: ['aoss:DescribeCollectionItems', 'aoss:CreateCollectionItems', 'aoss:UpdateCollectionItems'],
            },
            {
              ResourceType: 'index',
              Resource: [`index/${collectionName}/*`],
              Permission: [
                'aoss:CreateIndex',
                'aoss:DeleteIndex',
                'aoss:UpdateIndex',
                'aoss:DescribeIndex',
                'aoss:ReadDocument',
                'aoss:WriteDocument',
              ],
            },
          ],
          Principal: [`arn:aws:iam::${Stack.of(this).account}:root`],
        },
      ]),
    });
    accessPolicy.addDependency(collection);

    // --- FAQ Evidence Index (Custom Resource) ---
    const indexCreatorRole = new Role(this, 'IndexCreatorRole', {
      assumedBy: new ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [ManagedPolicy.fromAwsManagedPolicyName('service-role/AWSLambdaBasicExecutionRole')],
      inlinePolicies: {
        AossAccess: new PolicyDocument({
          statements: [
            new PolicyStatement({
              effect: Effect.ALLOW,
              actions: ['aoss:APIAccessAll'],
              resources: [collection.attrArn],
            }),
          ],
        }),
      },
    });

    const indexCreatorFn = new NodejsFunction(this, 'IndexCreatorFn', {
      entry: resolve(process.cwd(), 'lib', 'lambda', 'index-custom-resource-handler.ts'),
      handler: 'onEvent',
      runtime: Runtime.NODEJS_LATEST,
      role: indexCreatorRole,
      timeout: Duration.minutes(5),
      memorySize: 256,
      bundling: {
        externalModules: [],
        forceDockerBundling: false,
      },
    });

    const indexProvider = new Provider(this, 'IndexProvider', {
      onEventHandler: indexCreatorFn,
    });

    const faqEvidenceIndex = new CustomResource(this, 'FaqEvidenceIndex', {
      serviceToken: indexProvider.serviceToken,
      properties: {
        indexName: 'faq_evidence',
        collectionEndpoint: collection.attrCollectionEndpoint,
        indexBody: JSON.stringify({
          settings: { index: { knn: true } },
          mappings: {
            properties: {
              embedding: {
                type: 'knn_vector',
                dimension: 1024,
                method: {
                  engine: 'faiss',
                  name: 'hnsw',
                  space_type: 'cosinesimil',
                  parameters: { m: 24, ef_construction: 128 },
                },
              },
              fragment_id: { type: 'keyword' },
              source_id: { type: 'keyword' },
              text: { type: 'text' },
              source_url: { type: 'keyword', index: false },
              policy_links: { type: 'keyword', index: false },
              country: { type: 'keyword' },
              level: { type: 'keyword' },
              role: { type: 'keyword' },
              corpus_version: { type: 'keyword' },
              followup_fragment_ids: { type: 'keyword', index: false },
              // Promoted indexed filter field (T18): also in source_metadata, indexed here
              // as a first-class keyword field for fast query-time filtering. content_type is
              // the resolved value of the versioned COREx custom key (content-type-NN, highest
              // version wins).
              content_type: { type: 'keyword' },
              // Full preserved COREx metadata. flat_object stores arbitrarily-keyed content
              // (incl. versioned custom keys like content-type-16) as one field, so the index
              // mapping never explodes as authoring fields are renumbered. Only the body text
              // is embedded; this is provenance + future filter surface.
              source_metadata: { type: 'flat_object' },
            },
          },
        }),
      },
    });
    faqEvidenceIndex.node.addDependency(accessPolicy);

    // Cross-account read-only role for query service
    if (props.queryServiceAccountIds && props.queryServiceAccountIds.length > 0) {
      const queryRole = new Role(this, 'OpenSearchQueryRole', {
        roleName: `ATESkywalkerIngest-OpenSearchQueryRole-${props.stage}`,
        assumedBy: new AccountPrincipal(props.queryServiceAccountIds[0]),
      });
      // Allow additional accounts to assume the role.
      const trustPolicy = queryRole.assumeRolePolicy;
      if (trustPolicy === undefined) {
        throw new Error('OpenSearchQueryRole has no trust policy; expected a default one from assumedBy');
      }
      for (const accountId of props.queryServiceAccountIds.slice(1)) {
        trustPolicy.addStatements(
          new PolicyStatement({
            effect: Effect.ALLOW,
            principals: [new AccountPrincipal(accountId)],
            actions: ['sts:AssumeRole'],
          }),
        );
      }
      queryRole.addToPolicy(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ['aoss:APIAccessAll'],
          resources: [collection.attrArn],
        }),
      );

      // Read-only data access policy for the query role
      const queryAccessPolicy = new CfnAccessPolicy(this, 'QueryDataAccessPolicy', {
        name: `${collectionName}-query`,
        type: 'data',
        policy: JSON.stringify([
          {
            Description: 'Read-only access for cross-account query service',
            Rules: [
              {
                ResourceType: 'collection',
                Resource: [`collection/${collectionName}`],
                Permission: ['aoss:DescribeCollectionItems'],
              },
              {
                ResourceType: 'index',
                Resource: [`index/${collectionName}/*`],
                Permission: ['aoss:DescribeIndex', 'aoss:ReadDocument'],
              },
            ],
            Principal: [queryRole.roleArn],
          },
        ]),
      });
      queryAccessPolicy.addDependency(collection);

      new CfnOutput(this, 'QueryRoleArn', { value: queryRole.roleArn });
    }

    this.collectionEndpoint = collection.attrCollectionEndpoint;
    this.collectionArn = collection.attrArn;

    new CfnOutput(this, 'CollectionEndpoint', { value: collection.attrCollectionEndpoint });
    new CfnOutput(this, 'CollectionArn', { value: collection.attrArn });
  }
}
