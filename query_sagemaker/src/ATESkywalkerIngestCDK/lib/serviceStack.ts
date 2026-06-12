import { HydraTestRunResources, HydraBootstrapMode } from '@amzn/hydra';
import {
  BrazilPackage,
  DeploymentEnvironment,
  DeploymentStack,
  HydraTestApprovalWorkflowStep,
  LambdaAsset,
  Platform,
  SoftwareType,
} from '@amzn/pipelines';
import { Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Architecture, Function, Runtime } from 'aws-cdk-lib/aws-lambda';
import { Effect, PolicyStatement } from 'aws-cdk-lib/aws-iam';
import { Secret } from 'aws-cdk-lib/aws-secretsmanager';
import { StringParameter } from 'aws-cdk-lib/aws-ssm';
import { Rule, Schedule } from 'aws-cdk-lib/aws-events';
import { LambdaFunction } from 'aws-cdk-lib/aws-events-targets';

interface ServiceStackProps {
  readonly env: DeploymentEnvironment;
  readonly stage: string;
  readonly isProd: boolean; // TODO: re-used when adding prod stage (reserved concurrency, tighter alarm thresholds)
  readonly corexRoleArn?: string;
  readonly corexSessionPrefix?: string;
  readonly corexHost?: string;
  readonly corexFaqTopicId?: string;
  // Bedrock region and model ID for Cohere Embed v4 (ingestion-time embedding).
  // Defaults match the launch configuration per API_04.
  readonly bedrockRegion?: string;
  readonly bedrockModelId?: string;
  // Schedule for the daily ingestion run. When unset (e.g. personal stacks),
  // the Poller must be invoked manually.
  readonly scheduleExpression?: Schedule;
}

export class ServiceStack extends DeploymentStack {
  private readonly hydraResources: HydraTestRunResources;
  private readonly stage: string;
  // eslint-disable-next-line @typescript-eslint/ban-types
  public readonly pollerLambda: Function;
  // eslint-disable-next-line @typescript-eslint/ban-types
  public readonly processorLambda: Function;

  constructor(scope: Construct, id: string, props: ServiceStackProps) {
    super(scope, id, {
      env: props.env,
      softwareType: SoftwareType.LONG_RUNNING_SERVICE,
    });
    this.stage = props.stage;

    const bedrockRegion = props.bedrockRegion ?? 'us-west-2';
    const bedrockModelId = props.bedrockModelId ?? 'cohere.embed-v4:0';

    // Both Lambdas run at the Lambda hard cap. The Poller blocks on synchronous
    // parallel Processor invocations during a rebuild, so it needs the headroom.
    // The Processor chunks + embeds + bulk-writes one work item's nodes and
    // must tolerate Bedrock and OpenSearch latency spikes without timing out.
    this.pollerLambda = new Function(this, 'ATESkywalkerIngestPoller', {
      functionName: `ATESkywalkerIngest-Poller-${this.stage}`,
      description: 'Poller — fetches changed content nodeIds from COREx and dispatches to Processor.',
      code: LambdaAsset.fromBrazil({
        brazilPackage: BrazilPackage.fromString('ATESkywalkerIngest-1.0'),
        componentName: 'Lambda',
      }),
      handler: 'com.amazon.ingestion.lambda.poller.Poller::handleRequest',
      logGroup: new LogGroup(this, 'ATESkywalkerIngestPollerLogGroup', {
        retention: RetentionDays.ONE_YEAR,
      }),
      memorySize: 512,
      timeout: Duration.minutes(15),
      runtime: Runtime.JAVA_17,
      architecture: Architecture.ARM_64,
    });

    this.processorLambda = new Function(this, 'ATESkywalkerIngestProcessor', {
      functionName: `ATESkywalkerIngest-Processor-${this.stage}`,
      description: 'Processor — chunks content, embeds via Bedrock, writes to OpenSearch.',
      code: LambdaAsset.fromBrazil({
        brazilPackage: BrazilPackage.fromString('ATESkywalkerIngest-1.0'),
        componentName: 'Lambda',
      }),
      handler: 'com.amazon.ingestion.lambda.processor.Processor::handleRequest',
      logGroup: new LogGroup(this, 'ATESkywalkerIngestProcessorLogGroup', {
        retention: RetentionDays.ONE_YEAR,
      }),
      memorySize: 1024,
      timeout: Duration.minutes(15),
      runtime: Runtime.JAVA_17,
      architecture: Architecture.ARM_64,
    });

    // Poller can invoke Processor
    this.processorLambda.grantInvoke(this.pollerLambda);
    this.pollerLambda.addEnvironment('PROCESSOR_FUNCTION_NAME', this.processorLambda.functionName);

    // COREx access (optional — not available for personal stacks)
    if (props.corexRoleArn) {
      if (!props.corexSessionPrefix) {
        throw new Error('corexSessionPrefix is required when corexRoleArn is set');
      }
      const corexSessionPrefix = props.corexSessionPrefix;

      const corexSecret = Secret.fromSecretNameV2(this, 'CorexSecret', `ATESkywalkerIngest/${this.stage}/corex`);
      corexSecret.grantRead(this.pollerLambda);
      corexSecret.grantRead(this.processorLambda);

      this.pollerLambda.addEnvironment('COREX_ROLE_ARN', props.corexRoleArn);
      this.pollerLambda.addEnvironment('COREX_ROLE_SESSION_PREFIX', corexSessionPrefix);
      this.pollerLambda.addEnvironment('COREX_SECRET_NAME', `ATESkywalkerIngest/${this.stage}/corex`);

      this.processorLambda.addEnvironment('COREX_ROLE_ARN', props.corexRoleArn);
      this.processorLambda.addEnvironment('COREX_ROLE_SESSION_PREFIX', corexSessionPrefix);
      this.processorLambda.addEnvironment('COREX_SECRET_NAME', `ATESkywalkerIngest/${this.stage}/corex`);

      this.pollerLambda.addToRolePolicy(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ['sts:AssumeRole'],
          resources: [props.corexRoleArn],
        }),
      );

      this.processorLambda.addToRolePolicy(
        new PolicyStatement({
          effect: Effect.ALLOW,
          actions: ['sts:AssumeRole'],
          resources: [props.corexRoleArn],
        }),
      );
    }

    // COREx hostname per stage. Required whenever COREx role is wired.
    if (props.corexHost) {
      this.pollerLambda.addEnvironment('COREX_HOST', props.corexHost);
      this.processorLambda.addEnvironment('COREX_HOST', props.corexHost);
    }

    // FAQ topic UUID per stage. Poller uses this to filter SearchContent to the FAQ corpus.
    // Empty string is acceptable during scaffolding; the enumerator fails fast at runtime
    // if it is not populated before invocation.
    if (props.corexFaqTopicId !== undefined) {
      this.pollerLambda.addEnvironment('COREX_FAQ_TOPIC_ID', props.corexFaqTopicId);
    }

    // SSM parameters — Poller reads/writes poll timestamps
    const lastPollParam = StringParameter.fromStringParameterName(
      this,
      'LastPollTimestamp',
      `/ATESkywalkerIngest/${this.stage}/last-poll-timestamp`,
    );
    const lastContentUpdateParam = StringParameter.fromStringParameterName(
      this,
      'LastContentUpdateTimestamp',
      `/ATESkywalkerIngest/${this.stage}/last-content-update-timestamp`,
    );
    lastPollParam.grantRead(this.pollerLambda);
    lastPollParam.grantWrite(this.pollerLambda);
    lastContentUpdateParam.grantRead(this.pollerLambda);
    lastContentUpdateParam.grantWrite(this.pollerLambda);

    this.pollerLambda.addEnvironment(
      'SSM_LAST_POLL_TIMESTAMP',
      `/ATESkywalkerIngest/${this.stage}/last-poll-timestamp`,
    );
    this.pollerLambda.addEnvironment(
      'SSM_LAST_CONTENT_UPDATE_TIMESTAMP',
      `/ATESkywalkerIngest/${this.stage}/last-content-update-timestamp`,
    );

    // Bedrock — Cohere Embed v4 for ingestion-time embedding on the Processor.
    // Foundation-model ARNs have no account id: arn:aws:bedrock:{region}::foundation-model/{id}
    this.processorLambda.addEnvironment('BEDROCK_REGION', bedrockRegion);
    this.processorLambda.addEnvironment('BEDROCK_MODEL_ID', bedrockModelId);
    this.processorLambda.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [`arn:aws:bedrock:${bedrockRegion}::foundation-model/${bedrockModelId}`],
      }),
    );

    // EventBridge schedule — daily ingestion run invoking the Poller.
    // Personal stacks omit this; the Poller is invoked by hand there.
    if (props.scheduleExpression) {
      new Rule(this, 'DailyIngestionSchedule', {
        ruleName: `ATESkywalkerIngest-DailySchedule-${this.stage}`,
        description: 'Daily ingestion run — invokes the Poller Lambda.',
        schedule: props.scheduleExpression,
        targets: [new LambdaFunction(this.pollerLambda)],
      });
    }

    this.hydraResources = new HydraTestRunResources(this, 'HydraTestRunResources', {
      hydraEnvironment: props.env.hydraEnvironment,
      bootstrapMode: HydraBootstrapMode.AUTO,
      hydraAsset: {
        targetPackage: BrazilPackage.fromString('ATESkywalkerIngestTests-1.0'),
      },
    });

    this.pollerLambda.grantInvoke(this.hydraResources.invocationRole);
    this.processorLambda.grantInvoke(this.hydraResources.invocationRole);
  }

  createIntegrationTestsApprovalWorkflowStep(
    name: string,
    versionSetPlatform: Platform,
  ): HydraTestApprovalWorkflowStep {
    return this.hydraResources.createApprovalWorkflowStep({
      name,
      runDefinition: {
        SchemaVersion: '1.0',
        SchemaType: 'HydraJavaJUnit',
        HydraParameters: {
          Runtime: 'java17',
          ComputeEngine: 'Lambda',
        },
        HandlerParameters: {
          TestClasses: {
            PackageSelector: [
              {
                Package: 'com.amazon.ateskywalkeringest',
                ClassNamePattern: '.*Test',
              },
            ],
          },
        },
        EnvironmentVariables: {
          Stage: this.stage,
        },
      },
      versionSetPlatform,
    });
  }
}
