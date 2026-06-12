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
  readonly corexDomainOwnerId?: string;
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
    // Cohere Embed v4 is NOT invokable via the bare on-demand model id (cohere.embed-v4:0);
    // Bedrock requires a cross-region INFERENCE PROFILE. 'us.cohere.embed-v4:0' is the US
    // profile (routes within US regions). Pinned empirically — a bare-id InvokeModel returns
    // HTTP 400 "on-demand throughput isn't supported … use an inference profile."
    const bedrockModelId = props.bedrockModelId ?? 'us.cohere.embed-v4:0';

    // Both Lambdas run at the Lambda hard cap. The Poller blocks on synchronous
    // parallel Processor invocations during a rebuild, so it needs the headroom.
    // The Processor embeds and writes one fragment document per COREx node and
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
        // Retention per BuilderHub Lambda golden-path: set to org policy. SDO requires >=18mo
        // for security events; 24mo clears that floor for these operational logs. (AWS Security
        // recommends 10yr for security-event logs specifically — revisit if these logs are ever
        // classified as such.)
        retention: RetentionDays.TWO_YEARS,
      }),
      memorySize: 512,
      timeout: Duration.minutes(15),
      runtime: Runtime.JAVA_17,
      architecture: Architecture.ARM_64,
    });

    this.processorLambda = new Function(this, 'ATESkywalkerIngestProcessor', {
      functionName: `ATESkywalkerIngest-Processor-${this.stage}`,
      description: 'Processor — fetches COREx fragments, embeds via Bedrock, writes to OpenSearch.',
      code: LambdaAsset.fromBrazil({
        brazilPackage: BrazilPackage.fromString('ATESkywalkerIngest-1.0'),
        componentName: 'Lambda',
      }),
      handler: 'com.amazon.ingestion.lambda.processor.Processor::handleRequest',
      logGroup: new LogGroup(this, 'ATESkywalkerIngestProcessorLogGroup', {
        // Retention per BuilderHub Lambda golden-path: set to org policy. SDO requires >=18mo
        // for security events; 24mo clears that floor for these operational logs. (AWS Security
        // recommends 10yr for security-event logs specifically — revisit if these logs are ever
        // classified as such.)
        retention: RetentionDays.TWO_YEARS,
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

    // COREx domain owner Bindle ID per stage. Poller uses this to filter SearchContent
    // to content owned by Travel and Events.
    if (props.corexDomainOwnerId !== undefined) {
      this.pollerLambda.addEnvironment('COREX_DOMAIN_OWNER_ID', props.corexDomainOwnerId);
    }

    // SSM — the single high-water mark (R5): the most-recent lastModifiedDate ingested.
    // One value in one place; no per-node state, no separate poll heartbeat.
    const snapshotMarkerName = '/skywalker/ingestion/faq_evidence/last_snapshot_marker';
    const snapshotMarkerParam = StringParameter.fromStringParameterName(this, 'SnapshotMarker', snapshotMarkerName);
    snapshotMarkerParam.grantRead(this.pollerLambda);
    snapshotMarkerParam.grantWrite(this.pollerLambda);

    this.pollerLambda.addEnvironment('SSM_SNAPSHOT_MARKER', snapshotMarkerName);

    // SSM — the live-index pointer (T14, zero-downtime). AOSS Serverless does not support
    // index aliases, so we keep two physical indices (faq_evidence_a / _b) and this pointer
    // names the live one. The Poller flips it atomically after a verified, non-empty rebuild;
    // the query service reads it to resolve which index to query. The Poller also reads it at
    // the start of each run to pick the idle build target, so it needs read + write.
    const liveIndexName = '/skywalker/ingestion/faq_evidence/live_index';
    const liveIndexParam = StringParameter.fromStringParameterName(this, 'LiveIndex', liveIndexName);
    liveIndexParam.grantRead(this.pollerLambda);
    liveIndexParam.grantWrite(this.pollerLambda);

    this.pollerLambda.addEnvironment('SSM_LIVE_INDEX', liveIndexName);

    // Bedrock — Cohere Embed v4 for ingestion-time embedding on the Processor.
    // Embed v4 must be invoked through a cross-region inference profile (us.cohere.embed-v4:0),
    // not the bare model id. Cross-region profiles route to multiple US regions, so the role
    // needs InvokeModel on BOTH the inference-profile ARN (account-scoped) and the underlying
    // foundation-model ARNs in every region the profile can reach (us-east-1/2, us-west-2).
    // Inference-profile ARNs carry the account id; foundation-model ARNs do not.
    this.processorLambda.addEnvironment('BEDROCK_REGION', bedrockRegion);
    this.processorLambda.addEnvironment('BEDROCK_MODEL_ID', bedrockModelId);
    this.processorLambda.addToRolePolicy(
      new PolicyStatement({
        effect: Effect.ALLOW,
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:aws:bedrock:*:${this.account}:inference-profile/${bedrockModelId}`,
          'arn:aws:bedrock:*::foundation-model/cohere.embed-v4:0',
        ],
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
