import { App, Duration } from 'aws-cdk-lib';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';
import {
  BrazilPackage,
  DeploymentEnvironment,
  DeploymentPipeline,
  GordianKnotScannerApprovalWorkflowStep,
  Platform,
  ScanProfile,
  SchedulingGroupType,
} from '@amzn/pipelines';
import { PersonalStacks } from '@amzn/personal-stacks';
import { SUPPORTED_CDK_PROPERTY_INJECTORS_SDO } from '@amzn/secure-cdk-sdo-blueprint';
import { createIntegTest, createLoadTest } from './approval_workflow';
import { VpcStack } from './vpc';
import { EcsClusterStack } from './ecs_cluster';
import { ApolloShimStageType, EcsServiceStack } from './ecs_service';
import { MonitoringStack } from './monitoring';
import {
  ALPHA_ACCOUNT_ID,
  APPLICATION_ACCOUNT_ID,
  BETA_ACCOUNT_ID,
  GAMMA_ACCOUNT_ID,
  BINDLE_GUID,
  INGEST_ALPHA_ACCOUNT_ID,
  INGEST_BETA_ACCOUNT_ID,
  PIPELINE_ID,
  PIPELINE_NAME,
  SERVICE_NAME,
  TEAM_EMAIL,
  VERSION_SET,
} from './common/constants';
import { FoundationalResourcesStack } from './foundational_resources';

const app = new App({
  propertyInjectors: SUPPORTED_CDK_PROPERTY_INJECTORS_SDO,
});

const pipeline = new DeploymentPipeline(app, 'Pipeline', {
  account: APPLICATION_ACCOUNT_ID,
  pipelineName: PIPELINE_NAME,
  versionSet: VERSION_SET,
  versionSetPlatform: Platform.AL2023_AARCH64,
  trackingVersionSet: 'live',
  bindleGuid: BINDLE_GUID,
  description: 'Coral Service on Fargate',
  pipelineId: PIPELINE_ID,
  notificationEmailAddress: TEAM_EMAIL,
  selfMutate: true,
  createLegacyPipelineStage: false,
});
[
  'ATESkywalkerQuery-1.0/mainline',
  'ATESkywalkerQueryImageBuild-1.0/mainline',
  'ATESkywalkerQueryLogImageBuild-1.0/mainline',
  'ATESkywalkerQueryModel-1.0/mainline',
  'ATESkywalkerQueryClientConfig-1.0/mainline',
  'ATESkywalkerQueryJavaClient-1.0/mainline',
  'ATESkywalkerQueryTests-1.0/mainline',
].forEach((pkg) => pipeline.addPackageToAutobuild(BrazilPackage.fromString(pkg)));

pipeline.versionSetStage.addApprovalWorkflow('VersionSet Workflow').addStep(
  new GordianKnotScannerApprovalWorkflowStep({
    platform: Platform.AL2023_X86_64, // https://issues.amazon.com/issues/GK-1427
    scanProfileName: ScanProfile.ASSERT_LOW,
  }),
);

// We create an environment. An environment is where you want things deployed.
// You can deploy an arbitrary number of environments to an account/region
// Subject to account limits (role counts, bucket limits, etc)
// We recommend minimizing the number of things that share an account/region.
// It's almost never appropriate to mix a prod and beta environment.
// Naturally, you're welcome to wrap this in your choice of "for loops" or build generators etc
const alphaEnvironment = pipeline.deploymentEnvironmentFor(ALPHA_ACCOUNT_ID, 'us-west-2');
addPipelineStages(pipeline, 'alpha', ApolloShimStageType.Alpha, alphaEnvironment);

const betaEnvironment = pipeline.deploymentEnvironmentFor(BETA_ACCOUNT_ID, 'us-west-2');
addPipelineStages(pipeline, 'beta', ApolloShimStageType.Beta, betaEnvironment);

const gammaEnvironment = pipeline.deploymentEnvironmentFor(GAMMA_ACCOUNT_ID, 'us-east-1');
addPipelineStages(pipeline, 'gamma', ApolloShimStageType.Gamma, gammaEnvironment);

// Personal Stacks
const personalStacks = new PersonalStacks(app, {
  supportedDeploymentRegions: ['us-west-2'],
});
personalStacks.watch(pipeline, 'alpha-fleet');
addPipelineStages(
  pipeline,
  'personal',
  ApolloShimStageType.Alpha,
  personalStacks.app.deploymentEnvironment,
  personalStacks.app,
);

function addPipelineStages(
  pipeline: DeploymentPipeline,
  stageName: string,
  stageType: ApolloShimStageType,
  env: DeploymentEnvironment,
  scope: Construct = app,
  includeOnePod: boolean = stageType === ApolloShimStageType.Prod,
) {
  const isPersonalStack = scope !== app;
  const hydraTags = {
    privateSubnetTag: { key: 'HydraVpcPrivateSubnet', value: SERVICE_NAME },
    onePodSecurityGroupTag: { key: 'HydraSecurityGroup-OnePod', value: SERVICE_NAME },
    fleetSecurityGroupTag: { key: 'HydraSecurityGroup', value: SERVICE_NAME },
  };

  const vpc = new VpcStack(scope, `ATESkywalkerQuery-Vpc-${stageName}`, {
    env,
    hydraSubnetTag: hydraTags.privateSubnetTag,
    stage: stageName,
  });

  const prodStripe = { isProd: stageType == ApolloShimStageType.Prod };

  const listenerPortFleet = 443;
  const listenerPortOnePodOnly = 8443;
  const onePodWeightPercent = 5;
  const servicePort = 8443;
  const healthCheckPort = 8081;
  const minFleetTaskCount = 2;
  const maxFleetTaskCount = 20;
  const minOnePodTaskCount = Math.ceil((onePodWeightPercent * minFleetTaskCount) / 100);
  const maxOnePodTaskCount = Math.ceil((onePodWeightPercent * maxFleetTaskCount) / 100);

  const foundationalResources = new FoundationalResourcesStack(
    scope,
    `ATESkywalkerQuery-FoundationalResources-${stageName}`,
    {
      env,
      vpc: vpc.vpc,
      stage: stageName,
      listenerPortFleet,
      listenerPortOnePodOnly,
      onePodWeightPercent,
      servicePort,
      healthCheckPort,
      enableOnePod: includeOnePod,
    },
  );
  foundationalResources.addDependency(vpc);

  const cluster = new EcsClusterStack(scope, `ATESkywalkerQuery-EcsCluster-${stageName}`, {
    vpc: vpc.vpc,
    env,
  });
  cluster.addDependency(vpc);

  const serviceProps = {
    ecsCluster: cluster.cluster,
    loadBalancer: foundationalResources.loadBalancer,
    soaService: foundationalResources.soaService,
    env,
    metricsNamespace: SERVICE_NAME,
    servicePort,
    healthCheckPort,
    stageName,
    stageType,
    // Change to `true` to enable profiler in production
    enableProfiler: !prodStripe.isProd,
  };

  let onePodTarget;
  if (includeOnePod && foundationalResources.onePodResources) {
    const onePodServiceProps = {
      ...serviceProps,
      ...foundationalResources.onePodResources,
      isOnePod: true,
      hydraSecurityGroupTag: hydraTags.onePodSecurityGroupTag,
      minTaskCount: minOnePodTaskCount,
      maxTaskCount: maxOnePodTaskCount,
    };
    const onePodService = new EcsServiceStack(
      scope,
      `ATESkywalkerQuery-OnePodEcsService-${stageName}`,
      onePodServiceProps,
    );
    onePodService.addDependency(cluster);

    const onePodMonitoring = new MonitoringStack(scope, `ATESkywalkerQuery-OnePodMonitoring-${stageName}`, {
      env,
      logGroups: foundationalResources.onePodResources.logGroupMetricProps,
      targetGroup: foundationalResources.onePodResources.targetGroup,
      isOnePod: true,
      stageName,
      service: onePodService.service,
      metricsNamespace: SERVICE_NAME,
      loadBalancer: foundationalResources.loadBalancer,
    });
    onePodMonitoring.addDependency(onePodService);

    if (!isPersonalStack) {
      const onePodStage = pipeline.addStage(`${stageName}-onepod`, prodStripe);
      onePodTarget = onePodStage.addDeploymentGroup({
        name: `${stageName}-onepod`,
        stacks: [vpc, cluster, foundationalResources, onePodService, onePodMonitoring],
      });

      const onePodHydraVpcConfig = {
        SubnetTags: [hydraTags.privateSubnetTag],
        SecurityGroupTags: [hydraTags.onePodSecurityGroupTag],
      };
      const onePodIntegTest = createIntegTest(
        onePodServiceProps,
        cluster,
        onePodService.hydraResources,
        listenerPortOnePodOnly,
        onePodHydraVpcConfig,
      );
      const onePodLoadTest = createLoadTest(
        onePodServiceProps,
        cluster,
        onePodService.hydraResources,
        listenerPortOnePodOnly,
        onePodHydraVpcConfig,
      );
      onePodStage.addApprovalWorkflow('Approval Workflow', {
        sequence: [onePodIntegTest, onePodLoadTest],
        requiresConsistentRevisions: true,
        rollbackOnFailure: onePodStage.isProd,
      });
    }
  }

  const fleetServiceProps = {
    ...serviceProps,
    ...foundationalResources.fleetResources,
    isOnePod: false,
    hydraSecurityGroupTag: hydraTags.fleetSecurityGroupTag,
    minTaskCount: minFleetTaskCount,
    maxTaskCount: maxFleetTaskCount,
  };
  const fleetService = new EcsServiceStack(scope, `ATESkywalkerQuery-EcsService-${stageName}`, fleetServiceProps);
  fleetService.addDependency(cluster);

  // Allow task role to assume the OpenSearch query role in the ingest account
  // Prod ingest account not yet created — add when available
  const ingestAccountMap: Record<string, string> = {
    alpha: INGEST_ALPHA_ACCOUNT_ID,
    beta: INGEST_BETA_ACCOUNT_ID,
    gamma: INGEST_BETA_ACCOUNT_ID, // gamma shares beta's ingest collection
  };
  const ingestAccountId = ingestAccountMap[stageName];
  if (ingestAccountId) {
    const ingestRoleArn = `arn:aws:iam::${ingestAccountId}:role/ATESkywalkerIngest-OpenSearchQueryRole-${
      stageName === 'gamma' ? 'beta' : stageName
    }`;
    fleetService.taskDefinition.taskRole.addToPrincipalPolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['sts:AssumeRole'],
        resources: [ingestRoleArn],
      }),
    );
    // Tell the service which role to assume for cross-account AOSS retrieval.
    fleetService.container.addEnvironment('AOSS_ASSUME_ROLE_ARN', ingestRoleArn);
  }

  // Bedrock access for query embedding (Cohere Embed v4) and the on-demand
  // Rerank API (Cohere Rerank 3.5). Rerank requires both bedrock:Rerank and
  // bedrock:InvokeModel on the foundation model.
  fleetService.taskDefinition.taskRole.addToPrincipalPolicy(
    new PolicyStatement({
      effect: Effect.ALLOW,
      actions: ['bedrock:InvokeModel', 'bedrock:Rerank'],
      resources: [
        `arn:aws:bedrock:${env.region}::foundation-model/cohere.embed-v4:0`,
        `arn:aws:bedrock:${env.region}::foundation-model/cohere.rerank-v3-5:0`,
      ],
    }),
  );

  // SSM read access for calibration knobs (gate thresholds, budgets, timeouts).
  fleetService.taskDefinition.taskRole.addToPrincipalPolicy(
    new PolicyStatement({
      effect: Effect.ALLOW,
      actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
      resources: [`arn:aws:ssm:${env.region}:${env.account}:parameter/skywalker/query/*`],
    }),
  );

  const fleetMonitoring = new MonitoringStack(scope, `ATESkywalkerQuery-Monitoring-${stageName}`, {
    env,
    logGroups: foundationalResources.fleetResources.logGroupMetricProps,
    targetGroup: foundationalResources.fleetResources.targetGroup,
    isOnePod: false,
    stageName,
    service: fleetService.service,
    metricsNamespace: SERVICE_NAME,
    loadBalancer: foundationalResources.loadBalancer,
  });
  fleetMonitoring.addDependency(fleetService);

  if (!isPersonalStack) {
    const fleetStage = pipeline.addStage(`${stageName}-fleet`, prodStripe);
    const fleetStacks = includeOnePod
      ? [fleetService, fleetMonitoring]
      : [vpc, cluster, foundationalResources, fleetService, fleetMonitoring];
    const fleetTarget = fleetStage.addDeploymentGroup({
      name: `${stageName}-fleet`,
      stacks: fleetStacks,
      rollbackOnSubDeploymentFailure: true,
    });

    const fleetIntegTest = createIntegTest(fleetServiceProps, cluster, fleetService.hydraResources, listenerPortFleet, {
      SubnetTags: [hydraTags.privateSubnetTag],
      SecurityGroupTags: [hydraTags.fleetSecurityGroupTag],
    });
    fleetStage.addApprovalWorkflow('Approval Workflow', {
      sequence: [fleetIntegTest],
      requiresConsistentRevisions: true,
      rollbackOnFailure: fleetStage.isProd,
    });

    const targets = includeOnePod && onePodTarget ? [onePodTarget, fleetTarget] : [fleetTarget];
    pipeline.addSchedulingGroup({
      groupName: `${stageName}-ExclusiveGroup`,
      schedulingGroupType: SchedulingGroupType.EXCLUSIVE,
      timeInterval: Duration.minutes(0),
      targets,
    });
  }
}
