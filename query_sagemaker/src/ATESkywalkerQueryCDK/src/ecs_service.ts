import { Arn, ArnFormat, Duration, RemovalPolicy, Size, Tags } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { IService } from '@amzn/star-cdk-lib/soa';
import { PersonalStacks } from '@amzn/personal-stacks';
import { Fact } from 'aws-cdk-lib/region-info';
import { Port } from 'aws-cdk-lib/aws-ec2';
import {
  AwsLogDriverMode,
  ContainerDefinition,
  CpuArchitecture,
  ContainerDependencyCondition,
  FargatePlatformVersion,
  FargateService,
  FargateTaskDefinition,
  FirelensConfigFileType,
  FirelensLogRouter,
  FirelensLogRouterType,
  ICluster,
  LogDriver,
  LogDrivers,
} from 'aws-cdk-lib/aws-ecs';
import { ApplicationLoadBalancer, ApplicationTargetGroup } from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { ManagedPolicy, PolicyStatement, Role, ServicePrincipal } from 'aws-cdk-lib/aws-iam';
import { HydraTestRunResources, HydraBootstrapMode } from '@amzn/hydra';
import {
  BrazilContainerImage,
  BrazilPackage,
  DeploymentEnvironment,
  DeploymentStack,
  DogmaTagsOptions,
  HydraComputeEngine,
  SoftwareType,
} from '@amzn/pipelines';
import { ATESkywalkerQueryLogGroup } from './constructs/log_group';

export enum ApolloShimStageType {
  Alpha = 'alpha',
  Beta = 'beta',
  Gamma = 'gamma',
  Prod = 'prod',
}

export interface EcsServiceStackProps {
  readonly ecsCluster: ICluster;
  readonly loadBalancer: ApplicationLoadBalancer;
  readonly env: DeploymentEnvironment;
  readonly stackName?: string;
  readonly isOnePod: boolean;
  readonly soaService: IService;
  /**
   * Desired min task count of the service
   */
  readonly minTaskCount: number;
  /**
   * Desired max task count of the service
   */
  readonly maxTaskCount: number;
  /**
   * Tag added to the security group(s).
   * Used in the Hydra RunDefinition to run tests against VPC-only endpoint.
   */
  readonly hydraSecurityGroupTag: { key: string; value: string };
  /**
   * The namespace used to emit EMF metrics from your coral module.
   */
  readonly metricsNamespace: string;
  /**
   * Application container's port
   */
  readonly servicePort: number;
  /**
   * Application container's health check port, if different from servicePort
   */
  readonly healthCheckPort: number;
  /**
   * Stage name in log group names
   */
  readonly stageName: string;
  /**
   * Stage name in application container environment variables required by ApolloShim
   */
  readonly stageType: ApolloShimStageType;
  /**
   * Target group to register service instances to
   */
  readonly targetGroup: ApplicationTargetGroup;
  /**
   * Stack tags that will be applied to all the taggable resources and the stack itself.
   *
   * @default {}
   */
  readonly tags?: {
    [key: string]: string;
  };
  /**
   * Optional Dogma tags. Read `DogmaTags` for mode details or
   * this wiki https://w.amazon.com/bin/view/ReleaseExcellence/Team/Designs/PDGTargetSupport/Tags/
   */
  readonly dogmaTags?: DogmaTagsOptions;
  /**
   * Whether the application should be run with profiler enabled. Enabling to
   * identify performance bottlenecks or cost savings.
   *
   * @default - false, profiling is disabled by default.
   */
  readonly enableProfiler?: boolean;
}

export class EcsServiceStack extends DeploymentStack {
  public readonly service: FargateService;
  public readonly taskDefinition: FargateTaskDefinition;
  public readonly container: ContainerDefinition;
  public readonly hydraResources: HydraTestRunResources;

  constructor(parent: Construct, id: string, props: EcsServiceStackProps) {
    super(parent, id, {
      dogmaTags: props.dogmaTags,
      env: props.env,
      softwareType: SoftwareType.LONG_RUNNING_SERVICE,
      stackName: props.stackName,
      tags: props.tags,
    });

    const taskInstanceRole = this.createTaskInstanceRole();
    props.soaService.addIdentities({ role: taskInstanceRole });
    this.taskDefinition = new FargateTaskDefinition(this, 'TaskDefinition', {
      memoryLimitMiB: 1024,
      cpu: 512,
      runtimePlatform: {
        cpuArchitecture: PersonalStacks.isPersonalStack(this) ? CpuArchitecture.X86_64 : CpuArchitecture.ARM64,
      },
      taskRole: taskInstanceRole,
    });

    const extraJvmArgs: string[] = [];
    if (props.enableProfiler) {
      const profilingGroupName = props.isOnePod
        ? `ATESkywalkerQuery-${props.stageName}-OnePod-${props.env.region}`
        : `ATESkywalkerQuery-${props.stageName}-${props.env.region}`;
      extraJvmArgs.push(
        '-XX:+UnlockExperimentalVMOptions',
        '-XX:+StartProfiler',
        `-XX:ProfilerOptions=ProfilingGroupOverride=${profilingGroupName}`,
      );
    }

    this.container = this.taskDefinition.addContainer('Container', {
      image: BrazilContainerImage.fromBrazil({
        brazilPackage: BrazilPackage.fromString('ATESkywalkerQuery-1.0/mainline'),
        transformPackage: BrazilPackage.fromString('ATESkywalkerQueryImageBuild-1.0/mainline'),
        componentName: 'service',
      }),
      cpu: 256,
      memoryLimitMiB: 512,
      containerName: 'Application',
      logging: LogDrivers.firelens({}),
      environment: {
        IS_ONEPOD: props.isOnePod ? 'TRUE' : 'FALSE',
        NAMESPACE: props.metricsNamespace,
        STAGE: props.stageType,
        JVM_ARGS: extraJvmArgs.join(' '),
      },
    });
    this.container.addPortMappings({
      containerPort: props.servicePort,
    });
    this.container.addPortMappings({
      containerPort: props.healthCheckPort,
    });

    this.service = new FargateService(this, 'Service', {
      platformVersion: FargatePlatformVersion.VERSION1_4,
      cluster: props.ecsCluster,
      taskDefinition: this.taskDefinition,
      circuitBreaker: {
        enable: true,
      },
      minHealthyPercent: 100,
    });

    const scalable = this.service.autoScaleTaskCount({
      minCapacity: props.minTaskCount,
      maxCapacity: props.maxTaskCount,
    });
    scalable.scaleOnCpuUtilization('CpuScaling', {
      targetUtilizationPercent: 50,
      scaleInCooldown: Duration.seconds(900),
      scaleOutCooldown: Duration.seconds(60),
    });

    scalable.scaleOnMemoryUtilization('MemoryScaling', {
      targetUtilizationPercent: 50,
      scaleInCooldown: Duration.seconds(900),
      scaleOutCooldown: Duration.seconds(60),
    });

    this.service.connections.securityGroups.forEach((securityGroup) =>
      Tags.of(securityGroup).add(props.hydraSecurityGroupTag.key, props.hydraSecurityGroupTag.value),
    );

    this.service.connections.allowFrom(
      props.loadBalancer,
      Port.tcp(props.healthCheckPort),
      `Allow Load Balancer to connect to service on ${props.healthCheckPort}`,
    );

    props.targetGroup.addTarget(this.service);

    this.hydraResources = new HydraTestRunResources(this, 'HydraTestRunResources', {
      hydraEnvironment: props.env.hydraEnvironment,
      bootstrapMode: HydraBootstrapMode.AUTO,
      hydraAsset: {
        targetPackage: BrazilPackage.fromString('ATESkywalkerQueryTests-1.0/mainline'),
        engine: HydraComputeEngine.FARGATE,
      },
    });

    props.soaService.addIdentities({ role: this.hydraResources.invocationRole });

    const firelensLogGroup = new ATESkywalkerQueryLogGroup(this, 'FirelensLogGroup', {
      isOnePod: props.isOnePod,
      name: 'FirelensLogGroup',
      stage: props.stageName,
    });

    const fireLensContainer = this.addFireLensSidecar(props, firelensLogGroup);

    // Add dependencies between the firelens sidecar and the application container
    // so that the application container starts only after the firelens sidecar is healthy.
    this.container.addContainerDependencies({
      container: fireLensContainer,
      condition: ContainerDependencyCondition.HEALTHY,
    });
  }

  addFireLensSidecar(props: EcsServiceStackProps, firelensLogGroup: ATESkywalkerQueryLogGroup): FirelensLogRouter {
    // Add AWS for FluentBit sidecar container
    return this.taskDefinition.addFirelensLogRouter('FireLensContainer', {
      image: BrazilContainerImage.fromBrazil({
        brazilPackage: BrazilPackage.fromString('ATESkywalkerQueryLogImageBuild-1.0/mainline'),
        componentName: 'logging_container',
      }),
      cpu: 256,
      // Important! When changing the FluentBit memory limit, *also* update the
      // configuration to adjust each INPUT section's Mem_Buf_Limit, or the
      // total memory usage will not change.
      memoryLimitMiB: 512,
      environment: {
        CLOUDWATCH_ENDPOINT: Fact.requireFact(props.env.region, 'cloudwatchlogs.endpoint'),
        LOG_REGION: props.env.region,
        STAGE: props.stageName,
      },
      firelensConfig: {
        type: FirelensLogRouterType.FLUENTBIT,
        options: {
          enableECSLogMetadata: false,
          configFileType: FirelensConfigFileType.FILE,
          configFileValue: props.isOnePod ? '/config/fluent-bit-onepod.conf' : '/config/fluent-bit.conf',
        },
      },
      healthCheck: {
        command: ['CMD-SHELL', 'curl -f http://127.0.0.1:2020/api/v1/uptime || exit 1'],
      },
      logging: LogDriver.awsLogs({
        logGroup: firelensLogGroup.logGroup,
        streamPrefix: 'FireLens',
        mode: AwsLogDriverMode.NON_BLOCKING,
        maxBufferSize: Size.mebibytes(25),
      }),
    });
  }

  createTaskInstanceRole(): Role {
    const tasksPrincipal = ServicePrincipal.servicePrincipalName('ecs-tasks');

    const taskInstanceManagedPolicy = new ManagedPolicy(this, 'EcsTaskInstanceRoleManagedPolicy');

    const logging = new PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        Arn.format(
          {
            arnFormat: ArnFormat.COLON_RESOURCE_NAME,
            service: 'logs',
            resource: 'log-group',
            resourceName: `${ATESkywalkerQueryLogGroup.LOG_GROUP_NAME_PREFIX}*`,
          },
          this,
        ),
      ],
    });
    const metrics = new PolicyStatement({
      actions: ['cloudwatch:PutMetricData'],
      resources: ['*'],
    });

    // Bedrock: query embedding (Cohere Embed v4) and the Bedrock Rerank model used while
    // characterizing the reranker before committing to the self-hosted SageMaker endpoint.
    const bedrock = new PolicyStatement({
      actions: ['bedrock:InvokeModel'],
      resources: [
        Arn.format(
          {
            service: 'bedrock',
            account: '',
            resource: 'foundation-model',
            resourceName: 'cohere.embed-v4:0',
          },
          this,
        ),
        Arn.format(
          {
            service: 'bedrock',
            account: '',
            resource: 'foundation-model',
            resourceName: 'cohere.rerank-v3-5:0',
          },
          this,
        ),
      ],
    });

    // SageMaker: evidence reranker endpoint(s) (skywalker-rerank-*).
    const sagemaker = new PolicyStatement({
      actions: ['sagemaker:InvokeEndpoint'],
      resources: [Arn.format({ service: 'sagemaker', resource: 'endpoint', resourceName: 'skywalker-rerank-*' }, this)],
    });

    // SSM: calibration-active runtime parameters (gate thresholds, hybrid weights, budgets, timeouts).
    const ssm = new PolicyStatement({
      actions: ['ssm:GetParameter', 'ssm:GetParameters', 'ssm:GetParametersByPath'],
      resources: [Arn.format({ service: 'ssm', resource: 'parameter', resourceName: 'skywalker/runtime/*' }, this)],
    });

    taskInstanceManagedPolicy.addStatements(logging, metrics, bedrock, sagemaker, ssm);
    taskInstanceManagedPolicy.applyRemovalPolicy(RemovalPolicy.DESTROY);

    return new Role(this, 'EcsTaskInstanceRole', {
      assumedBy: new ServicePrincipal(tasksPrincipal),
      managedPolicies: [taskInstanceManagedPolicy],
    });
  }
}
