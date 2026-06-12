#!/usr/bin/env node
import { App } from 'aws-cdk-lib';
import { Schedule } from 'aws-cdk-lib/aws-events';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';

import {
  DependencyModel,
  DeploymentPipeline,
  GordianKnotScannerApprovalWorkflowStep,
  Platform,
  ScanProfile,
} from '@amzn/pipelines';
import { BrazilPackage } from '@amzn/pipelines';
import { PersonalStacks } from '@amzn/personal-stacks';
import { ServiceStack } from './serviceStack';
import { MonitoringStack } from './monitoringStack';
import { OpenSearchStack } from './openSearchStack';

// Set up your CDK App
const app = new App();

const applicationAccount = '048679569136';

const pipeline = new DeploymentPipeline(app, 'Pipeline', {
  account: applicationAccount,
  pipelineName: 'ATESkywalkerIngest',
  versionSet: {
    name: 'ATESkywalkerIngest/development',
    dependencyModel: DependencyModel.BRAZIL,
  },
  versionSetPlatform: Platform.AL2_AARCH64,
  trackingVersionSet: 'live', // Or any other version set you prefer
  bindleGuid: 'amzn1.bindle.resource.4sd57vyhrm2kuglcmxfq',
  description: 'Java Lambda basic pipeline managed by CDK',
  notificationEmailAddress: 'nmatnich@amazon.com',
  pipelineId: '9340894',
  selfMutate: true,
  createLegacyPipelineStage: false,
});

['ATESkywalkerIngest', 'ATESkywalkerIngestTests'].forEach((pkg) =>
  pipeline.addPackageToAutobuild(BrazilPackage.fromString(pkg)),
);

pipeline.versionSetStage.addApprovalWorkflow('VersionSet Workflow').addStep(
  new GordianKnotScannerApprovalWorkflowStep({
    platform: Platform.AL2_X86_64, // https://issues.amazon.com/issues/GK-956
    scanProfileName: ScanProfile.ASSERT_LOW,
  }),
);

// Daily ingestion schedule. 08:00 UTC is a launch default — tune per stage
// once we know when the COREx corpus is typically updated.
const dailyIngestionSchedule = Schedule.cron({ minute: '0', hour: '8' });

const stageName = 'alpha';
const alphaStage = pipeline.addStage(stageName, { isProd: false });
const deploymentGroup = alphaStage.addDeploymentGroup({
  name: 'alphaApplication',
});

const env = pipeline.deploymentEnvironmentFor('948580600005', 'us-west-2');

const serviceStack = new ServiceStack(app, `ATESkywalkerIngest-Service-${stageName}`, {
  env,
  stage: alphaStage.name,
  isProd: alphaStage.isProd,
  corexRoleArn: 'arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta',
  corexSessionPrefix: 'ATESkywalker',
  corexHost: 'corex-api.beta.corex.pxt.amazon.dev',
  // TODO: populate once the FAQ domain owner provides the alpha FAQ topic UUID
  // from the COREx Beta instance. Until then the enumerator fails fast at runtime.
  corexFaqTopicId: '',
  scheduleExpression: dailyIngestionSchedule,
});
const monitoringStack = new MonitoringStack(app, `ATESkywalkerIngest-Monitoring-${stageName}`, {
  env,
  pollerLambda: serviceStack.pollerLambda,
  processorLambda: serviceStack.processorLambda,
});
const openSearchStack = new OpenSearchStack(app, `ATESkywalkerIngest-OpenSearch-${stageName}`, {
  env,
  stage: alphaStage.name,
  isProd: alphaStage.isProd,
  queryServiceAccountIds: ['465556393784'], // ateskywalkerquery-app-alpha
});
deploymentGroup.addStacks(serviceStack, monitoringStack, openSearchStack);

serviceStack.processorLambda.addEnvironment('OPENSEARCH_ENDPOINT', openSearchStack.collectionEndpoint);
serviceStack.processorLambda.addToRolePolicy(
  new PolicyStatement({
    effect: Effect.ALLOW,
    actions: ['aoss:APIAccessAll'],
    resources: [openSearchStack.collectionArn],
  }),
);
serviceStack.pollerLambda.addEnvironment('OPENSEARCH_ENDPOINT', openSearchStack.collectionEndpoint);
serviceStack.pollerLambda.addToRolePolicy(
  new PolicyStatement({
    effect: Effect.ALLOW,
    actions: ['aoss:APIAccessAll'],
    resources: [openSearchStack.collectionArn],
  }),
);

const hydraApproval = serviceStack.createIntegrationTestsApprovalWorkflowStep('Integration Test', Platform.AL2_AARCH64);

alphaStage.addApprovalWorkflow('Approval Workflow', {
  sequence: [hydraApproval],
  requiresConsistentRevisions: true,
});

// Beta stage
const betaStageName = 'beta';
const betaStage = pipeline.addStage(betaStageName, { isProd: false });
const betaDeploymentGroup = betaStage.addDeploymentGroup({
  name: 'betaApplication',
});

const betaEnv = pipeline.deploymentEnvironmentFor('334296258454', 'us-west-2');

const betaServiceStack = new ServiceStack(app, `ATESkywalkerIngest-Service-${betaStageName}`, {
  env: betaEnv,
  stage: betaStage.name,
  isProd: betaStage.isProd,
  corexRoleArn: 'arn:aws:iam::703004971069:role/preProdATESkywalker-CORExApiRole-prod',
  corexSessionPrefix: 'preProdATESkywalker',
  corexHost: 'corex-api.corex.pxt.amazon.dev',
  // TODO: populate once the FAQ domain owner provides the beta FAQ topic UUID
  // from the COREx PreProd instance. Until then the enumerator fails fast at runtime.
  corexFaqTopicId: '',
  scheduleExpression: dailyIngestionSchedule,
});
const betaMonitoringStack = new MonitoringStack(app, `ATESkywalkerIngest-Monitoring-${betaStageName}`, {
  env: betaEnv,
  pollerLambda: betaServiceStack.pollerLambda,
  processorLambda: betaServiceStack.processorLambda,
});
const betaOpenSearchStack = new OpenSearchStack(app, `ATESkywalkerIngest-OpenSearch-${betaStageName}`, {
  env: betaEnv,
  stage: betaStage.name,
  isProd: betaStage.isProd,
  queryServiceAccountIds: ['278522729570', '817294254658'], // ateskywalkerquery-app-beta, gamma
});
betaDeploymentGroup.addStacks(betaServiceStack, betaMonitoringStack, betaOpenSearchStack);

betaServiceStack.processorLambda.addEnvironment('OPENSEARCH_ENDPOINT', betaOpenSearchStack.collectionEndpoint);
betaServiceStack.processorLambda.addToRolePolicy(
  new PolicyStatement({
    effect: Effect.ALLOW,
    actions: ['aoss:APIAccessAll'],
    resources: [betaOpenSearchStack.collectionArn],
  }),
);
betaServiceStack.pollerLambda.addEnvironment('OPENSEARCH_ENDPOINT', betaOpenSearchStack.collectionEndpoint);
betaServiceStack.pollerLambda.addToRolePolicy(
  new PolicyStatement({
    effect: Effect.ALLOW,
    actions: ['aoss:APIAccessAll'],
    resources: [betaOpenSearchStack.collectionArn],
  }),
);

const betaHydraApproval = betaServiceStack.createIntegrationTestsApprovalWorkflowStep(
  'Integration Test',
  Platform.AL2_AARCH64,
);

betaStage.addApprovalWorkflow('Approval Workflow', {
  sequence: [betaHydraApproval],
  requiresConsistentRevisions: true,
});

// Personal Stacks
const personalStacks = new PersonalStacks(app, {
  supportedDeploymentRegions: ['us-west-2'],
});

personalStacks.watch(pipeline, 'alpha');

// Personal stacks share alpha's COREx configuration (COREx Beta, cross-account
// role in 975754358161). The developer populates Secrets Manager at
// ATESkywalkerIngest/personal/corex with their own ApiKey/ExternalId pair.
// No EventBridge schedule — the Poller is invoked manually in personal stacks.
const personalServiceStack = new ServiceStack(personalStacks.app, 'ATESkywalkerIngest-Service-personal', {
  env: personalStacks.app.deploymentEnvironment,
  stage: 'personal',
  isProd: false,
  corexRoleArn: 'arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta',
  corexSessionPrefix: 'ATESkywalker',
  corexHost: 'corex-api.beta.corex.pxt.amazon.dev',
  // TODO: populate once the FAQ domain owner provides the alpha FAQ topic UUID
  // from the COREx Beta instance. Until then the enumerator fails fast at runtime.
  corexFaqTopicId: '',
});

new MonitoringStack(personalStacks.app, 'ATESkywalkerIngest-Monitoring-personal', {
  env: personalStacks.app.deploymentEnvironment,
  pollerLambda: personalServiceStack.pollerLambda,
  processorLambda: personalServiceStack.processorLambda,
});
