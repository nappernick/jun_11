import { RemovalPolicy } from 'aws-cdk-lib';
import { LogGroup, RetentionDays } from 'aws-cdk-lib/aws-logs';
import { Construct } from 'constructs';

export interface ATESkywalkerQueryLogGroupProps {
  /**
   * Whether the log group is for the OnePod service
   * If true, adds the ONEPOD_LOG_GROUP_PREFIX to the name of the log group
   *
   * @default false
   */
  readonly isOnePod?: boolean;

  /**
   * Name of the log group
   */
  readonly name: string;

  /**
   * The pipeline stage
   */
  readonly stage: string;

  /**
   * Removal policy
   *
   * @default RemovalPolicy.RETAIN
   */
  readonly removalPolicy?: RemovalPolicy;

  /**
   * How long log contents will be retained
   *
   * @default RetentionDays.TEN_YEARS
   */
  readonly retentionDays?: RetentionDays;
}

export class ATESkywalkerQueryLogGroup extends Construct {
  /**
   * The log_group_name configuration parameter in the LogImageBuild package must match the
   * name of the log group created through this construct. Be sure to update those parameters
   * when making changes to the constants or the createLogGroupName method in this class.
   */
  static readonly LOG_GROUP_NAME_PREFIX = 'ATESkywalkerQuery';
  static readonly ONEPOD_LOG_GROUP_PREFIX = 'OnePod';
  readonly logGroup: LogGroup;

  constructor(parent: Construct, id: string, props: ATESkywalkerQueryLogGroupProps) {
    super(parent, id);

    const logGroupName = this.createLogGroupName(props);

    this.logGroup = new LogGroup(this, logGroupName, {
      logGroupName: logGroupName,
      removalPolicy: props.removalPolicy ?? RemovalPolicy.RETAIN,
      retention: props.retentionDays ?? RetentionDays.TEN_YEARS,
    });
  }

  createLogGroupName(props: ATESkywalkerQueryLogGroupProps) {
    const segments = [ATESkywalkerQueryLogGroup.LOG_GROUP_NAME_PREFIX];
    if (props.isOnePod) {
      segments.push(ATESkywalkerQueryLogGroup.ONEPOD_LOG_GROUP_PREFIX);
    }
    segments.push(props.stage, props.name);
    return segments.join('-');
  }
}
