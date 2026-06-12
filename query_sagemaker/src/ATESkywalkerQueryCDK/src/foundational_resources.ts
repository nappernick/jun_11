import { IService, PendingApprovalAction, Service } from '@amzn/star-cdk-lib/soa';
import { ServiceEnvironment } from '@amzn/superstar-provisioner-cdk';
import { PersonalStacks } from '@amzn/personal-stacks';

import {
  BINDLE_GUID,
  DELEGATION_ROLE_PREFIX,
  DNS_DELEGATION_ACCOUNT,
  HOSTED_ZONE_NAME,
  SERVICE_NAME,
} from './common/constants';
import { ALLEGIANCE_DNS, ALLEGIANCE_HOSTED_ZONE_IDS } from './common/allegiance_config';
import { AccountPrincipal, Effect, PolicyDocument, PolicyStatement, Role } from 'aws-cdk-lib/aws-iam';
import { CrossAccountZoneDelegationRecord, IHostedZone, PublicHostedZone } from 'aws-cdk-lib/aws-route53';
import { Certificate, CertificateValidation } from 'aws-cdk-lib/aws-certificatemanager';
import { Arn, Duration, RemovalPolicy } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import { IVpc, Peer, Port, SecurityGroup, VpcEndpointService } from 'aws-cdk-lib/aws-ec2';
import {
  ApplicationLoadBalancer,
  ApplicationProtocol,
  ApplicationTargetGroup,
  DesyncMitigationMode,
  ListenerAction,
  NetworkLoadBalancer,
  Protocol as ElbProtocol,
  TargetType,
  SslPolicy,
  NetworkTargetGroup,
  Protocol,
} from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { ARecord, RecordTarget, PrivateHostedZone } from 'aws-cdk-lib/aws-route53';
import { AliasRecordTargetConfig } from 'aws-cdk-lib/aws-route53';
import { BlockPublicAccess, Bucket, BucketEncryption, StorageClass } from 'aws-cdk-lib/aws-s3';
import { DeploymentEnvironment, DeploymentStack, SoftwareType } from '@amzn/pipelines';
import { ATESkywalkerQueryLogGroup } from './constructs/log_group';
import { LoadBalancerTarget } from 'aws-cdk-lib/aws-route53-targets';
import { LogGroupMetricProps } from './monitoring';
import { AlbArnTarget } from 'aws-cdk-lib/aws-elasticloadbalancingv2-targets';

export interface FoundationalResourcesStackProps {
  readonly env: DeploymentEnvironment;
  readonly vpc: IVpc;
  /**
   * Port for main service listener
   */
  readonly listenerPortFleet: number;
  /**
   * Port to create second listener that only targets the OnePod service
   * Needed for OnePod Hydra test
   */
  readonly listenerPortOnePodOnly: number;
  /**
   * Value from 0-100 for weighted target groups configuration
   */
  readonly onePodWeightPercent: number;
  /**
   * Application container's port
   */
  readonly servicePort: number;
  /**
   * Application container's health check port
   */
  readonly healthCheckPort: number;
  /**
   * Add stage to private service endpoint domain name
   */
  readonly stage: string;
  /**
   * Whether to enable OnePod infrastructure (separate target group, weighted routing).
   * When false, all traffic goes to fleet only.
   * @default false
   */
  readonly enableOnePod?: boolean;
}

export interface EcsServiceResources {
  readonly logGroupMetricProps: LogGroupMetricProps[];
  readonly targetGroup: ApplicationTargetGroup;
}

export class FoundationalResourcesStack extends DeploymentStack {
  readonly soaService: IService;
  readonly loadBalancer: ApplicationLoadBalancer;
  readonly networkLoadBalancer: NetworkLoadBalancer;
  readonly fleetResources: EcsServiceResources;
  readonly onePodResources?: EcsServiceResources;
  public readonly hostedZone: IHostedZone;

  constructor(parent: Construct, id: string, props: FoundationalResourcesStackProps) {
    super(parent, id, {
      softwareType: SoftwareType.INFRASTRUCTURE,
      ...props,
    });

    // ==========
    //  DNS Setup
    // ==========
    this.hostedZone = this.createDnsDelegation(props.stage, props.env.region);
    const domainName = this.hostedZone.zoneName;
    const acmCertificate = new Certificate(this, 'Certificate', {
      domainName,
      certificateName: 'ATESkywalkerQuery Certificate', // Optionally provide an certificate name
      validation: CertificateValidation.fromDns(this.hostedZone),
    });

    // ================
    //  SuperStar Setup
    // ================

    // https://w.amazon.com/bin/view/SuperStar/Provisioner/GettingStarted/SuperStarCDK/
    const isPersonalStack = PersonalStacks.isPersonalStack(this);
    const serviceEnvironment = new ServiceEnvironment(this, 'SuperStar', {
      bindleGUID: BINDLE_GUID,
      name: `${SERVICE_NAME}${isPersonalStack ? `-${PersonalStacks.personalStackId(this)?.replace(/-/g, '')}` : ''}`,
      stage: isPersonalStack ? 'alpha' : props.stage,
    });
    serviceEnvironment.within(props.vpc);

    // ====================
    //  Load Balancer Setup
    // ====================

    const enableOnePod = props.enableOnePod ?? false;

    if (enableOnePod && (props.onePodWeightPercent < 0 || props.onePodWeightPercent > 100)) {
      throw new Error('You must specify the OnePod target group weight between 0 and 100 for the OnePod service');
    }
    this.loadBalancer = this.createLoadBalancer(props.vpc);
    const targetGroupFleet = this.createTargetGroup(props.vpc, props.servicePort, '', props.healthCheckPort);
    const targetGroupOnePod = enableOnePod
      ? this.createTargetGroup(props.vpc, props.servicePort, 'OnePod', props.healthCheckPort)
      : undefined;

    const albListener = this.loadBalancer.addListener('Listener', {
      defaultAction:
        enableOnePod && targetGroupOnePod
          ? ListenerAction.weightedForward([
              { targetGroup: targetGroupOnePod, weight: props.onePodWeightPercent },
              { targetGroup: targetGroupFleet, weight: 100 - props.onePodWeightPercent },
            ])
          : ListenerAction.forward([targetGroupFleet]),
      protocol: ApplicationProtocol.HTTPS,
      port: props.listenerPortFleet,
      certificates: [acmCertificate],
      sslPolicy: SslPolicy.RECOMMENDED_TLS,
    });
    this.networkLoadBalancer = this.createNetworkLoadBalancer(props.vpc);
    const networkLoadBalancerTargetGroup = this.createNetworkLoadBalancerTargetGroup(
      props.vpc,
      props.listenerPortFleet,
      '',
    );
    networkLoadBalancerTargetGroup.node.addDependency(albListener);
    this.networkLoadBalancer.addListener('SecureNLBListener', {
      defaultTargetGroups: [networkLoadBalancerTargetGroup],
      port: props.listenerPortFleet, // 443
      protocol: Protocol.TCP, // L3 protocol to proxy to alb
    });

    const vpcEndpointService = this.createVpcEndpointService(this.networkLoadBalancer);

    // Create Allegiance IAM Role for CORP-to-NAWS PrivateLink connectivity
    this.createAllegianceRole();

    if (enableOnePod && targetGroupOnePod) {
      const onePodOnlyListener = this.loadBalancer.addListener('OnePodOnlyListener', {
        defaultTargetGroups: [targetGroupOnePod],
        open: false,
        protocol: ApplicationProtocol.HTTPS,
        port: props.listenerPortOnePodOnly,
        certificates: [acmCertificate],
        sslPolicy: SslPolicy.RECOMMENDED_TLS,
      });
      onePodOnlyListener.connections.allowFrom(
        Peer.ipv4(props.vpc.vpcCidrBlock).connections,
        Port.tcp(props.listenerPortOnePodOnly),
        'Restrict OnePod-only listener access to VPC connections',
      );
    }

    // Public DNS → Allegiance endpoint (for CORP callers like MCP Gateway)
    const allegianceDns = ALLEGIANCE_DNS[props.stage]?.[props.env.region];
    const allegianceHostedZoneId = ALLEGIANCE_HOSTED_ZONE_IDS[props.env.region];

    if (allegianceDns && allegianceHostedZoneId) {
      new ARecord(this, 'ServiceAliasRecord', {
        zone: this.hostedZone,
        recordName: domainName,
        target: RecordTarget.fromAlias({
          bind(): AliasRecordTargetConfig {
            return {
              dnsName: allegianceDns,
              hostedZoneId: allegianceHostedZoneId,
            };
          },
        }),
        comment: 'Allegiance endpoint for CORP/MCP Gateway access',
      });
    } else {
      // Fallback: point directly to ALB (for stages not yet onboarded to Allegiance)
      new ARecord(this, 'ServiceAliasRecord', {
        zone: this.hostedZone,
        target: RecordTarget.fromAlias(new LoadBalancerTarget(this.loadBalancer)),
        recordName: domainName,
      });
    }

    // Private DNS → ALB (for Hydra tests and internal VPC access)
    const privateZone = new PrivateHostedZone(this, 'PrivateHostedZone', {
      zoneName: domainName,
      vpc: props.vpc,
    });
    new ARecord(this, 'PrivateARecord', {
      zone: privateZone,
      target: RecordTarget.fromAlias(new LoadBalancerTarget(this.loadBalancer)),
      comment: 'Internal A record for Hydra tests and VPC access',
    });

    // ==========
    //  SOA Setup
    // ==========

    this.soaService = new Service(this, 'SOAService', {
      bindleName: 'Amazon-Travel-Events-Software-Skywalker-Query',
      serviceName: SERVICE_NAME,
    });
    this.soaService.attachVpc(props.vpc);

    this.soaService.addOperations({ name: 'CreateBeer' }, { name: 'GetAllBeers' });

    // Adding a dependency to the service itself in order to enable integration tests
    this.soaService.dependsOn({
      serviceName: SERVICE_NAME,
      operationNames: ['CreateBeer', 'GetAllBeers'],
      // `IGNORE` means the CloudFormation deployment will kick off a relationship
      // request workflow but will not fail if the relationship is not already approved.
      // If we prefer the deployment to be blocked on relationship request approval,
      // change it to `ABORT`. But the best practice here is to verify the availability
      // of dependencies in service's integration tests.
      pendingApprovalAction: PendingApprovalAction.IGNORE,
    });

    this.soaService.addEndpoints({
      domainNames: [domainName],
      vpcEndpointService: vpcEndpointService,
    });

    // ========================
    //  Listener Resource Setup
    // ========================

    this.fleetResources = {
      logGroupMetricProps: [],
      targetGroup: targetGroupFleet,
    };

    if (enableOnePod && targetGroupOnePod) {
      this.onePodResources = {
        logGroupMetricProps: [],
        targetGroup: targetGroupOnePod,
      };
    }

    this.createLogGroups(props.stage, enableOnePod);
  }

  createDnsDelegation(stage: string, region: string): IHostedZone {
    const subDomain = `${region}-${stage}`;

    const hostedZone = new PublicHostedZone(this, 'HostedZone', {
      zoneName: `${subDomain}.${HOSTED_ZONE_NAME}`,
    });

    // Import the delegation role by constructing the roleArn
    const delegationRoleArn = Arn.format({
      partition: 'aws',
      service: 'iam',
      region: '', // IAM is global in each partition
      account: DNS_DELEGATION_ACCOUNT,
      resource: 'role',
      // Must match the role name created in the DNS pipeline's SubDomains stack
      resourceName: `${DELEGATION_ROLE_PREFIX}-${subDomain}`,
    });

    // Create the record which is delegated from the root domain
    new CrossAccountZoneDelegationRecord(this, 'DelegationRecord', {
      delegatedZone: hostedZone,
      parentHostedZoneName: HOSTED_ZONE_NAME,
      delegationRole: Role.fromRoleArn(this, 'DnsRole', delegationRoleArn),
    });

    return hostedZone;
  }

  // The names of the log groups created here must match the log_group_name configuration
  // parameter in the LogImageBuild package. Be sure to update those parameters when making
  // changes to the names in this class.
  createLogGroups(stage: string, enableOnePod: boolean) {
    [
      { name: 'AppContainer-STDOUT', alarm: false },
      { name: 'ApplicationLogs', alarm: false },
      { name: 'RequestLogs', alarm: true, dataOnlyWhenRequests: true },
      { name: 'ServiceMetrics', alarm: true, dataOnlyWhenRequests: false },
    ].forEach((logGroup) => {
      if (enableOnePod && this.onePodResources) {
        const onePodLogGroup = new ATESkywalkerQueryLogGroup(this, `OnePod-${logGroup.name}`, {
          isOnePod: true,
          name: logGroup.name,
          stage: stage,
        });

        if (logGroup.alarm) {
          this.onePodResources.logGroupMetricProps.push({
            dataOnlyWhenRequests: logGroup.dataOnlyWhenRequests,
            logGroupName: onePodLogGroup.logGroup.logGroupPhysicalName(),
          });
        }
      }

      const fleetLogGroup = new ATESkywalkerQueryLogGroup(this, logGroup.name, {
        isOnePod: false,
        name: logGroup.name,
        stage: stage,
      });

      if (!logGroup.alarm) {
        return;
      }

      this.fleetResources.logGroupMetricProps.push({
        dataOnlyWhenRequests: logGroup.dataOnlyWhenRequests,
        logGroupName: fleetLogGroup.logGroup.logGroupPhysicalName(),
      });
    });
  }

  createLoadBalancer(vpc: IVpc): ApplicationLoadBalancer {
    const albSecurityGroup = new SecurityGroup(this, `ALBSecurityGroup`, {
      vpc,
    });
    albSecurityGroup.addIngressRule(Peer.ipv4(vpc.vpcCidrBlock), Port.tcp(443));
    const loadBalancer = new ApplicationLoadBalancer(this, 'LoadBalancer', {
      deletionProtection: true,
      desyncMitigationMode: DesyncMitigationMode.STRICTEST,
      dropInvalidHeaderFields: true,
      internetFacing: false,
      vpc,
      xAmznTlsVersionAndCipherSuiteHeaders: true,
      securityGroup: albSecurityGroup,
    });

    /**
     * ARC Zonal Shift allows you to manually shift traffic away from a single Availability Zone in the event of a AZ outage.
     * The feature allows you to automatically shift traffic away from a single AZ in the event of an outage, if you enable
     * AutoShift on your Load Balancer. Enabling AutoShift requires you enable practice runs, which will simulate a AZ outage at random
     * times. If you enable AutoShift and Practice runs it is important that you schedule Block Days
     * to prevent a practice run during large events.
     *
     * ARC Zonal Shift team is tracking a solution to this for SDO:
     * https://sim.amazon.com/issues/MRP-34469
     *
     * You can see how to enable AutoShift in the following CR:
     * https://code.amazon.com/reviews/CR-184944581
     *
     * You can read more about practice runs here:
     * https://docs.aws.amazon.com/r53recovery/latest/dg/arc-zonal-autoshift.considerations.html#ZAConsiderationsPracticeRunAlarms
     */

    loadBalancer.setAttribute('zonal_shift.config.enabled', 'true');

    const accessLogsBucket = this.createAccessLogsBuckets();
    loadBalancer.logAccessLogs(accessLogsBucket);
    return loadBalancer;
  }

  createAccessLogsBuckets(): Bucket {
    const bucketProps = {
      blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
      encryption: BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      lifecycleRules: [
        {
          id: 'ExpireAfterOneYear',
          enabled: true,
          expiration: Duration.days(366),
          noncurrentVersionExpiration: Duration.days(366),
          noncurrentVersionTransitions: [
            {
              storageClass: StorageClass.GLACIER,
              transitionAfter: Duration.days(7),
            },
          ],
        },
      ],
    };

    const serverAccessLogsBucket = new Bucket(this, 'ServerAccessLogsBucket', {
      ...bucketProps,
    });

    return new Bucket(this, 'LoadBalancerAccessLogsBucket', {
      ...bucketProps,
      serverAccessLogsBucket: serverAccessLogsBucket,
    });
  }

  createTargetGroup(vpc: IVpc, port: number, prefix = '', healthCheckPort: number): ApplicationTargetGroup {
    return new ApplicationTargetGroup(this, `${prefix}TargetGroup`, {
      deregistrationDelay: Duration.seconds(60),
      healthCheck: {
        path: '/ping',
        port: healthCheckPort.toString(),
        protocol: ElbProtocol.HTTP,
      },
      port: port,
      targetType: TargetType.IP,
      vpc,
    });
  }
  private createNetworkLoadBalancer(vpc: IVpc): NetworkLoadBalancer {
    const lbSecurityGroup = new SecurityGroup(this, `NLBSecurityGroup`, {
      vpc,
    });
    lbSecurityGroup.addIngressRule(Peer.ipv4(vpc.vpcCidrBlock), Port.tcp(443));
    return new NetworkLoadBalancer(this, 'NetworkLoadBalancer', {
      vpc,
      crossZoneEnabled: true,
      securityGroups: [lbSecurityGroup],
      enforceSecurityGroupInboundRulesOnPrivateLinkTraffic: false,
    });
  }
  /**
   * create a NetworkTargetGroup which points to the alb
   */
  private createNetworkLoadBalancerTargetGroup(vpc: IVpc, port: number, prefix = ''): NetworkTargetGroup {
    return new NetworkTargetGroup(this, `${prefix}NetworkTargetGroup`, {
      port,
      vpc,
      targetType: TargetType.ALB,
      healthCheck: {
        path: '/ping',
        port: 'traffic-port',
        protocol: ElbProtocol.HTTPS,
      },
      targets: [new AlbArnTarget(this.loadBalancer.loadBalancerArn, port)],
    });
  }

  private createVpcEndpointService(networkLoadBalancer: NetworkLoadBalancer): VpcEndpointService {
    return new VpcEndpointService(this, 'VpcEndpointService', {
      vpcEndpointServiceLoadBalancers: [networkLoadBalancer],
    });
  }

  private createAllegianceRole(): void {
    const role = new Role(this, 'AllegianceRole', {
      roleName: 'AllegianceClientAccess-DO-NOT-DELETE',
      assumedBy: new AccountPrincipal('113025808262'),
      inlinePolicies: {
        AllegianceInboundPrivateLink: new PolicyDocument({
          statements: [
            new PolicyStatement({
              sid: 'AllegianceInboundPrivateLink',
              effect: Effect.ALLOW,
              actions: [
                'ec2:AcceptVpcEndpointConnections',
                'ec2:CreateVpcEndpoint',
                'ec2:DeleteVpcEndpoints',
                'ec2:DescribeSubnets',
                'ec2:DescribeVpcs',
                'ec2:DescribeVpcEndpointConnections',
                'ec2:DescribeVpcEndpoints',
                'ec2:DescribeVpcEndpointServiceConfigurations',
                'ec2:DescribeVpcEndpointServicePermissions',
                'ec2:DescribeVpcEndpointServices',
                'ec2:ModifyVpcEndpoint',
                'ec2:ModifyVpcEndpointServicePermissions',
                'ec2:RejectVpcEndpointConnections',
                'elasticloadbalancing:DescribeLoadBalancers',
                'elasticloadbalancing:CreateListener',
                'elasticloadbalancing:CreateLoadBalancer',
                'elasticloadbalancing:CreateTargetGroup',
                'elasticloadbalancing:DeleteListener',
                'elasticloadbalancing:DeleteLoadBalancer',
                'elasticloadbalancing:DeleteTargetGroup',
                'elasticloadbalancing:DeregisterTargets',
              ],
              resources: ['*'],
            }),
          ],
        }),
      },
    });
    role.applyRemovalPolicy(RemovalPolicy.RETAIN);
  }
}
