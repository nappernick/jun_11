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
import { FAQ_EVIDENCE_INDEX_NAME, FAQ_EVIDENCE_INDEX_BODY } from './faqEvidenceIndexMapping';

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
              Permission: ['aoss:DescribeCollectionItems'],
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
      entry: resolve(__dirname, '..', '..', 'lib', 'lambda', 'index-custom-resource-handler.ts'),
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
        indexName: FAQ_EVIDENCE_INDEX_NAME,
        collectionEndpoint: collection.attrCollectionEndpoint,
        indexBody: JSON.stringify(FAQ_EVIDENCE_INDEX_BODY),
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
