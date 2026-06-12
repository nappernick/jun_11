import { Construct } from 'constructs';
import { Cluster, ContainerInsights, ICluster } from 'aws-cdk-lib/aws-ecs';
import { IVpc } from 'aws-cdk-lib/aws-ec2';
import { DeploymentEnvironment, DeploymentStack, DogmaTagsOptions, SoftwareType } from '@amzn/pipelines';

export interface EcsClusterStackProps {
  readonly vpc: IVpc;
  readonly env: DeploymentEnvironment;
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

export class EcsClusterStack extends DeploymentStack {
  readonly cluster: ICluster;

  constructor(parent: Construct, id: string, props: EcsClusterStackProps) {
    super(parent, id, {
      softwareType: SoftwareType.INFRASTRUCTURE,
      dogmaTags: props.dogmaTags,
      env: props.env,
      stackName: props.stackName,
      tags: props.tags,
    });
    // CloudFormation Resources
    this.cluster = new Cluster(this, 'Cluster', {
      vpc: props.vpc,
      containerInsightsV2: ContainerInsights.ENABLED,
    });
  }
}
