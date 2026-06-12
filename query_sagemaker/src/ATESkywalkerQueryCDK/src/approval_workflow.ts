import { HydraTestRunResources } from '@amzn/hydra';
import { HydraTestApprovalWorkflowStep, Platform } from '@amzn/pipelines';
import { EcsClusterStack } from './ecs_cluster';
import { EcsServiceStackProps } from './ecs_service';

export interface HydraVpcConfig {
  readonly SubnetTags: { key: string; value: string }[];
  readonly SecurityGroupTags: { key: string; value: string }[];
}

export function createIntegTest(
  serviceProps: EcsServiceStackProps,
  cluster: EcsClusterStack,
  hydraResources: HydraTestRunResources,
  listenerPort: number,
  hydraVpcConfig: HydraVpcConfig,
): HydraTestApprovalWorkflowStep {
  // Hydra Test Run Definition, which defines parameters to run the test step.
  // See: https://builderhub.corp.amazon.com/docs/hydra/user-guide/concepts-run-definition.html
  const runDefinition = {
    SchemaVersion: '1.0',
    SchemaType: 'HydraJavaTestNG',
    HydraParameters: {
      ComputeEngine: 'Fargate',
      Network: hydraVpcConfig,
      Runtime: 'java21',
    },
    EnvironmentVariables: {
      JAVA_TOOL_OPTIONS: '-Dlog4j.configurationFile=log4j/HydraTestPlatformDefaultLog4j.xml',
      CORAL_CONFIG_PATH: 'coral-config',
      domain: serviceProps.stageName,
      region: serviceProps.env.region,
      onepod: serviceProps.isOnePod.toString(),
    },
    HandlerParameters: {
      tests: [
        {
          name: 'IntegrationTest',
          packages: [
            {
              name: 'com.amazon.ateskywalkerquery',
            },
          ],
        },
      ],
    },
  };

  return hydraResources.createApprovalWorkflowStep({
    name: createApprovalWorkflowStepName(serviceProps.isOnePod, 'Integ Test', serviceProps.env.region),
    runDefinition,
    versionSetPlatform: Platform.AL2023_X86_64,
  });
}

export function createLoadTest(
  serviceProps: EcsServiceStackProps,
  cluster: EcsClusterStack,
  hydraResources: HydraTestRunResources,
  listenerPort: number,
  hydraVpcConfig: HydraVpcConfig,
): HydraTestApprovalWorkflowStep {
  // Hydra Test Run Definition, which defines parameters to run the test step.
  // See: https://builderhub.corp.amazon.com/docs/hydra/user-guide/concepts-run-definition.html
  const runDefinition = {
    SchemaVersion: '1.0',
    SchemaType: 'HydraJavaTestNG',
    HydraParameters: {
      Runtime: 'java21',
      ComputeEngine: 'Fargate',
      CpuUnits: 2048,
      MemorySize: 4096,
      Metrics: { Enabled: true },
      Network: hydraVpcConfig,
      TestVertical: 'LOADTEST',
      Series: [
        {
          Rate: '50/PT1S', // Execute the test suite 50 times per second
          Duration: 'PT10M', // 10 mins
          ApprovalHeuristics: [
            {
              Name: 'global_completion_rate',
              Threshold: '100.0',
            },
            {
              Name: 'fail_global_error_rate',
              Threshold: '5.0', // 95% availability
            },
          ],
        },
      ],
    },
    EnvironmentVariables: {
      JAVA_TOOL_OPTIONS: '-Dlog4j.configurationFile=log4j/HydraTestPlatformDefaultLog4j.xml',
      CORAL_CONFIG_PATH: 'coral-config',
      domain: serviceProps.stageName,
      region: serviceProps.env.region,
      onepod: serviceProps.isOnePod.toString(),
    },
    HandlerParameters: {
      tests: [
        {
          name: 'LoadTest',
          packages: [
            {
              name: 'com.amazon.ateskywalkerquery',
            },
          ],
        },
      ],
    },
  };

  return hydraResources.createApprovalWorkflowStep({
    name: createApprovalWorkflowStepName(serviceProps.isOnePod, 'Load Test', serviceProps.env.region),
    runDefinition,
    versionSetPlatform: Platform.AL2023_X86_64,
  });
}

function createApprovalWorkflowStepName(isOnePod: boolean, name: string, region: string): string {
  const workflowStepPrefix = isOnePod ? 'OnePod ' : '';
  return `${workflowStepPrefix}${name} - ${region}`;
}
