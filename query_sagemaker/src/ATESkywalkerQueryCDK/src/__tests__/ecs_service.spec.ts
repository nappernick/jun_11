import { test } from 'vitest';
import { Template } from 'aws-cdk-lib/assertions';
import { DeploymentEnvironmentFactory } from '@amzn/pipelines';
import { App } from 'aws-cdk-lib';
import { ApolloShimStageType, EcsServiceStack } from '../ecs_service';
import { EcsClusterStack } from '../ecs_cluster';
import { VpcStack } from '../vpc';
import { FoundationalResourcesStack } from '../foundational_resources';

test('create expected ECS Resources', () => {
  const mockApp = new App();
  const deploymentEnv = DeploymentEnvironmentFactory.fromAccountAndRegion('test-account', 'us-west-2', 'unique-id');
  const stage = 'Beta';
  const servicePort = 8443;
  const listenerPortFleet = 443;
  const listenerPortOnePodOnly = 8443;
  const healthCheckPort = 8081;
  const vpcStack = new VpcStack(mockApp, 'vpcid', {
    stage: stage,
    env: deploymentEnv,
    hydraSubnetTag: {
      key: 'HydraVpcPrivateSubnet',
      value: 'MockApplication',
    },
  });

  const foundationalResourcesStack = new FoundationalResourcesStack(mockApp, `foundationalresources`, {
    env: deploymentEnv,
    vpc: vpcStack.vpc,
    listenerPortFleet,
    listenerPortOnePodOnly,
    onePodWeightPercent: 5,
    servicePort,
    healthCheckPort: healthCheckPort,
    stage: stage,
  });

  const ecsClusterStack = new EcsClusterStack(mockApp, 'ecsclusterid', {
    vpc: vpcStack.vpc,
    env: deploymentEnv,
  });

  const ecsServiceStack = new EcsServiceStack(mockApp, 'ecsserviceid', {
    stageName: stage,
    stageType: ApolloShimStageType.Beta,
    env: deploymentEnv,
    ecsCluster: ecsClusterStack.cluster,
    loadBalancer: foundationalResourcesStack.loadBalancer,
    soaService: foundationalResourcesStack.soaService,
    isOnePod: false,
    metricsNamespace: 'ATESkywalkerQuery',
    servicePort: servicePort,
    healthCheckPort: healthCheckPort,
    targetGroup: foundationalResourcesStack.fleetResources.targetGroup,
    hydraSecurityGroupTag: {
      key: 'HydraSecurityGroup-OnePod',
      value: 'MockApplication',
    },
    minTaskCount: 1,
    maxTaskCount: 2,
  });
  const template = Template.fromStack(ecsServiceStack);
  template.hasResource('AWS::ECS::Service', {});
});
