import { Duration, Tags } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  FlowLog,
  FlowLogDestination,
  FlowLogResourceType,
  GatewayVpcEndpointAwsService,
  InterfaceVpcEndpointAwsService,
  IpAddresses,
  LogFormat,
  Vpc,
} from 'aws-cdk-lib/aws-ec2';
import {
  DeploymentEnvironment,
  DeploymentStack,
  DeploymentStackAZBehavior as AZBehavior,
  SoftwareType,
  DogmaTagsOptions,
} from '@amzn/pipelines';
import { BlockPublicAccess, Bucket, BucketEncryption, StorageClass } from 'aws-cdk-lib/aws-s3';
import { Key } from 'aws-cdk-lib/aws-kms';
import { RetentionDays } from 'aws-cdk-lib/aws-logs';

// If you want to add parameters for your CDK Stack, you can toss them in here
export interface VpcStackProps {
  readonly env: DeploymentEnvironment;
  /**
   * Name of pipeline stage.
   */
  readonly stage: string;
  /**
   * Tag added to the VPC's private subnets.
   * Used in the Hydra RunDefinition to run tests against VPC-only endpoint.
   */
  readonly hydraSubnetTag: { key: string; value: string };
  readonly stackName?: string;
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
}

// CDK context is needed to determine which VPC endpoints can
// be added to the Availability Zones in your account. It is not
// possible to fetch CDK context in ADC regions, so add those
// to those list if you deploy into any.
const REGIONS_WITHOUT_CONTEXT = [
  'us-west-2',
  'us-east-1', // gamma region — lets CloudFormation resolve AZs at deploy time
];

export class VpcStack extends DeploymentStack {
  public readonly vpc: Vpc;

  constructor(parent: Construct, id: string, props: VpcStackProps) {
    super(parent, id, {
      softwareType: SoftwareType.INFRASTRUCTURE,
      azBehavior: REGIONS_WITHOUT_CONTEXT.includes(props.env.region) ? AZBehavior.INTRINSIC : AZBehavior.CONTEXT,
      ...props,
    });
    this.vpc = new Vpc(this, 'Vpc', {
      ipAddresses: IpAddresses.cidr('10.0.0.0/16'),
    });
    this.vpc.privateSubnets.forEach((subnet) =>
      Tags.of(subnet).add(props.hydraSubnetTag.key, props.hydraSubnetTag.value),
    );

    this.enableFlowLogs();
    this.addEndpoints();
  }

  addEndpoints() {
    const endpoints: Array<[string, InterfaceVpcEndpointAwsService]> = [
      ['CloudWatch', InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING],
      ['CloudWatchLogs', InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS],
      ['Ecr', InterfaceVpcEndpointAwsService.ECR],
      ['EcrDocker', InterfaceVpcEndpointAwsService.ECR_DOCKER],
    ];
    for (const [name, service] of endpoints) {
      this.vpc.addInterfaceEndpoint(`VpcEndpoint${name}`, {
        service,
        privateDnsEnabled: true,
        lookupSupportedAzs: !this.useIntrinsicAZs,
      });
    }

    const gateways: Array<[string, GatewayVpcEndpointAwsService]> = [
      ['S3', GatewayVpcEndpointAwsService.S3],
      ['DynamoDB', GatewayVpcEndpointAwsService.DYNAMODB],
    ];
    for (const [name, service] of gateways) {
      this.vpc.addGatewayEndpoint(`GatewayEndpoint${name}`, {
        service,
      });
    }
  }

  enableFlowLogs() {
    const vpcFlowLogsKey = new Key(this, 'VPCFlowLogsKey', {
      enableKeyRotation: true,
    });

    const vpcFlowLogsAccessLogEncryptionKey = new Key(this, 'VPCFlowLogsAccessLogsBucketEncryptionKey', {
      enableKeyRotation: true,
    });

    const vpcFlowLogsAccessLogsBucket = new Bucket(this, 'VPCFlowLogsBucketAccessLogsBucket', {
      encryption: BucketEncryption.KMS,
      encryptionKey: vpcFlowLogsAccessLogEncryptionKey,
      bucketKeyEnabled: true,
      versioned: true,
      lifecycleRules: [
        {
          id: 'ExpireAfterTenYears',
          enabled: true,
          // Security logging standard: https://policy.a2z.com/docs/247/publication
          expiration: Duration.days(RetentionDays.TEN_YEARS),
          noncurrentVersionExpiration: Duration.days(RetentionDays.TEN_YEARS),
          noncurrentVersionTransitions: [
            {
              storageClass: StorageClass.GLACIER,
              transitionAfter: Duration.days(7),
            },
          ],
        },
      ],
      enforceSSL: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
    });

    const vpcFlowLogsBucket = new Bucket(this, 'VPCFlowLogsBucket', {
      encryption: BucketEncryption.KMS,
      encryptionKey: vpcFlowLogsKey,
      bucketKeyEnabled: true,
      versioned: true,
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      serverAccessLogsBucket: vpcFlowLogsAccessLogsBucket,
      lifecycleRules: [
        {
          id: 'Tiered storage',
          transitions: [
            {
              storageClass: StorageClass.GLACIER,
              transitionAfter: Duration.days(90),
            },
          ],
          abortIncompleteMultipartUploadAfter: Duration.days(1),
          noncurrentVersionExpiration: Duration.days(7),
          // Security logging standard: https://policy.a2z.com/docs/247/publication
          expiration: Duration.days(RetentionDays.TEN_YEARS),
        },
      ],
    });

    new FlowLog(this, 'VPCFlowLogs', {
      resourceType: FlowLogResourceType.fromVpc(this.vpc),
      logFormat: [
        /**
         * Fields in the default log format
         * https://docs.aws.amazon.com/vpc/latest/userguide/flow-log-records.html#flow-logs-fields
         */
        LogFormat.VERSION,
        LogFormat.ACCOUNT_ID,
        LogFormat.INTERFACE_ID,
        LogFormat.SRC_ADDR,
        LogFormat.SRC_PORT,
        LogFormat.DST_ADDR,
        LogFormat.DST_PORT,
        LogFormat.PROTOCOL,
        LogFormat.PACKETS,
        LogFormat.BYTES,
        LogFormat.START_TIMESTAMP,
        LogFormat.END_TIMESTAMP,
        LogFormat.ACTION,
        LogFormat.LOG_STATUS,

        // Extra useful fields
        LogFormat.PKT_SRC_ADDR,
        LogFormat.PKT_DST_ADDR,
        LogFormat.PKT_SRC_AWS_SERVICE,
        LogFormat.PKT_DST_AWS_SERVICE,
        LogFormat.TRAFFIC_PATH,
      ],
      destination: FlowLogDestination.toS3(vpcFlowLogsBucket),
    });
  }
}
