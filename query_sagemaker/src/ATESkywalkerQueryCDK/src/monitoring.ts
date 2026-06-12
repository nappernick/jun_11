import { DeploymentEnvironment, DeploymentStack, DogmaTagsOptions, SoftwareType } from '@amzn/pipelines';
import { Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  Alarm,
  AlarmRule,
  AlarmWidget,
  ComparisonOperator,
  CompositeAlarm,
  Dashboard,
  IAlarm,
  IMetric,
  IWidget,
  MathExpression,
  Metric,
  PeriodOverride,
  Stats,
  TextWidget,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { FargateService } from 'aws-cdk-lib/aws-ecs';
import { ApplicationLoadBalancer, ApplicationTargetGroup } from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import { CfnQueryDefinition } from 'aws-cdk-lib/aws-logs';

export interface LogGroupMetricProps {
  /**
   * Amount of time over which metric statistic is measured.
   *
   * @default 1 minute
   */
  readonly period?: Duration;
  /**
   * If true, the log group is only written to when there are requests being made to the service.
   *
   * @default false
   */
  readonly dataOnlyWhenRequests?: boolean;
  /**
   * Name of the log group to alarm on.
   */
  readonly logGroupName: string;
}

export interface MonitoringStackProps {
  readonly isOnePod: boolean;
  /**
   * List of log groups to monitor for missing data along with the settings to use for each log group's metric.
   */
  readonly logGroups: LogGroupMetricProps[];
  /**
   * Target group of the service that will get requests.
   * Used with the log groups that will only have data written to them when there are requests to the service.
   */
  readonly targetGroup: ApplicationTargetGroup;
  readonly env: DeploymentEnvironment;
  readonly stageName: string;
  readonly service: FargateService;
  readonly metricsNamespace: string;
  readonly loadBalancer: ApplicationLoadBalancer;
  /**
   * Optional Dogma tags. Read `DogmaTags` for mode details or
   * this wiki https://w.amazon.com/bin/view/ReleaseExcellence/Team/Designs/PDGTargetSupport/Tags/
   */
  readonly dogmaTags?: DogmaTagsOptions;
}

interface Alarming {
  readonly loadBalancerErrorRateAlarm: IAlarm;
  readonly loadBalancerFaultRateAlarm: IAlarm;
  readonly loadBalancerTargetFaultRateAlarm: IAlarm;
  readonly memoryUtilizationAlarm: IAlarm;
  readonly cpuUtilizationAlarm: IAlarm;
  readonly heapMemoryAlarm: IAlarm;
  readonly heapMemoryHighSeverityAlarm: IAlarm;
  readonly trafficIntensityAlarm: IAlarm;
  readonly noFileAlarm: IAlarm;
  readonly noFileHighSeverityAlarm: IAlarm;
  readonly diskUtilizationAlarm: IAlarm;
  readonly diskUtilizationHighSeverityAlarm: IAlarm;
  readonly logGroupAlarms: IAlarm[];
}

interface Operation {
  /**
   * Name of the operation.
   */
  readonly name: string;
  /**
   * Latency in milliseconds that will trigger an alarm.
   */
  readonly latencyAlarmThreshold: number;
}

export class MonitoringStack extends DeploymentStack {
  public readonly serviceAggregateHealthAlarm: CompositeAlarm;
  public readonly aggregateAlarmPeriodMinutes: number;
  public readonly defaultAlarmPeriod: Duration = Duration.minutes(1);
  private readonly suffix: string;
  private readonly serviceNamespace: string;
  private readonly alarmList: Alarming;
  /**
   * Coral currently does not support programmatically obtaining the list of operations
   * related to a Coral service without hacky methods (https://issues.amazon.com/issues/CORAL-1510)
   * So they need to be manually defined here. As you add operations to your service, add
   * them to the operations list.
   */
  private static readonly operations: Set<Operation> = new Set<Operation>([
    {
      name: 'CreateBeer',
      latencyAlarmThreshold: 200,
    },
    {
      name: 'BSFPing',
      latencyAlarmThreshold: 50,
    },
  ]);
  private static readonly textWidgetProps = {
    width: 24,
    height: 1,
  };
  private static readonly alarmWidgetProps = {
    width: 24,
    height: 6,
  };
  private static readonly alarmWidgetMsAxis = {
    leftYaxis: {
      min: 0,
      label: 'ms',
      showUnits: true,
    },
  };
  private static readonly alarmWidgetPercentAxis = {
    leftYaxis: {
      min: 0,
      label: '%',
      showUnits: true,
    },
  };

  constructor(parent: Construct, id: string, private readonly props: MonitoringStackProps) {
    super(parent, id, {
      softwareType: SoftwareType.INFRASTRUCTURE,
      ...props,
    });

    this.suffix = props.isOnePod ? '-OnePod' : '';
    this.serviceNamespace = `${props.metricsNamespace}${this.suffix}`;

    if (props.logGroups.length === 0) {
      throw new Error('There must be at least one log group to alarm on');
    }

    this.alarmList = this.createAlarms();

    const alarmPeriod = Math.max(
      ...props.logGroups.map((logGroup) => (logGroup.period ?? this.defaultAlarmPeriod).toMinutes()),
    );
    this.aggregateAlarmPeriodMinutes = alarmPeriod;

    this.serviceAggregateHealthAlarm = new CompositeAlarm(this, `${this.serviceNamespace}-AggregateHealthAlarm`, {
      compositeAlarmName: `${this.serviceNamespace}-AggregateHealthAlarm`,
      alarmDescription: 'One or more CloudWatch alarms were breached.',
      alarmRule: AlarmRule.anyOf(...Object.values(this.alarmList).flat()),
    });

    this.createServiceDashboard(this.alarmList);

    if (!props.isOnePod) {
      this.createOverallDashboard(this.alarmList);
    }

    // Create CloudWatch Insights query definitions
    this.createCloudWatchLogsInsightsQueries();
  }

  private createOperationWidgets(): IWidget[] {
    const widgets: IWidget[] = [];

    MonitoringStack.operations.forEach((operation) => {
      const faultRateAlarm = this.createApplicationFaultRateAlarm(operation);
      const faultCountAlarm = this.createApplicationFaultCountAlarm(operation);
      const latencyAlarm = this.createApplicationLatencyAlarm(operation);

      widgets.push(
        new TextWidget({
          ...MonitoringStack.textWidgetProps,
          markdown: `### ${operation.name}`,
        }),
        new AlarmWidget({
          ...MonitoringStack.alarmWidgetProps,
          ...MonitoringStack.alarmWidgetPercentAxis,
          title: 'Fault Rate',
          alarm: faultRateAlarm,
        }),
        new AlarmWidget({
          ...MonitoringStack.alarmWidgetProps,
          title: 'Fault Count',
          alarm: faultCountAlarm,
          leftYAxis: {
            min: 0,
            label: 'Count',
            showUnits: true,
          },
        }),
        new AlarmWidget({
          ...MonitoringStack.alarmWidgetProps,
          ...MonitoringStack.alarmWidgetMsAxis,
          title: 'Latency',
          alarm: latencyAlarm,
        }),
      );
    });

    return widgets;
  }

  private createOverallDashboard(alarms: Alarming): Dashboard {
    const overallDashboard = new Dashboard(this, 'OverallDashboard', {
      dashboardName: `${this.props.metricsNamespace}-${this.props.env.region}-${this.props.stageName}-Overall`,
      start: '-' + Duration.days(14).toIsoString(),
      periodOverride: PeriodOverride.INHERIT,
    });

    overallDashboard.addWidgets(
      new TextWidget({
        ...MonitoringStack.textWidgetProps,
        markdown: '# Overall dashboard',
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'ALB Error Rate',
        alarm: alarms.loadBalancerErrorRateAlarm,
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'ALB Fault Rate',
        alarm: alarms.loadBalancerFaultRateAlarm,
      }),
    );
    return overallDashboard;
  }

  private createServiceDashboard(alarms: Alarming): Dashboard {
    const serviceDashboard = new Dashboard(this, 'ServiceDashboard', {
      dashboardName: `${this.serviceNamespace}-${this.props.env.region}-${this.props.stageName}-Service`,
      start: '-' + Duration.hours(8).toIsoString(),
      periodOverride: PeriodOverride.INHERIT,
    });

    const logGroupWidgets: AlarmWidget[] = alarms.logGroupAlarms.map((logAlarm) => {
      return new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        title: logAlarm.alarmName,
        alarm: logAlarm,
        leftYAxis: {
          min: 0,
          label: 'Events',
          showUnits: true,
        },
      });
    });

    serviceDashboard.addWidgets(
      new TextWidget({
        ...MonitoringStack.textWidgetProps,
        markdown: '# Service dashboard',
      }),
      new TextWidget({
        ...MonitoringStack.textWidgetProps,
        markdown: '## Overall',
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'Memory Utilization',
        alarm: alarms.memoryUtilizationAlarm,
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'CPU Utilization',
        alarm: alarms.cpuUtilizationAlarm,
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'Heap Memory Utilization',
        alarm: alarms.heapMemoryAlarm,
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'Traffic Intensity',
        alarm: alarms.trafficIntensityAlarm,
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'Disk Utilization',
        alarm: alarms.diskUtilizationAlarm,
      }),
      new AlarmWidget({
        ...MonitoringStack.alarmWidgetProps,
        ...MonitoringStack.alarmWidgetPercentAxis,
        title: 'File Descriptor Utilization',
        alarm: alarms.noFileAlarm,
      }),
      new TextWidget({
        ...MonitoringStack.textWidgetProps,
        markdown: '## Operations',
      }),
      ...this.createOperationWidgets(),
      new TextWidget({
        ...MonitoringStack.textWidgetProps,
        markdown: '# Log Groups',
      }),
      ...logGroupWidgets,
    );
    return serviceDashboard;
  }

  createALBErrorRateAlarm(): IAlarm {
    const albErrorCountMetric = this.createALBErrorCountMetric();
    const albRequestCountMetric = this.createALBRequestCountMetric();
    return new Alarm(this, 'ALBErrorRateAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-ALBErrorRateAlarm`,
      metric: new MathExpression({
        expression: 'errorCount / requestCount * 100',
        usingMetrics: {
          errorCount: albErrorCountMetric,
          requestCount: albRequestCountMetric,
        },
        period: this.defaultAlarmPeriod,
        label: 'Error Rate',
      }),
      threshold: 0.5,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
  }

  createALBFaultRateAlarm(): IAlarm {
    const albFaultCountMetric = this.createALBFaultCountMetric();
    const albRequestCountMetric = this.createALBRequestCountMetric();
    return new Alarm(this, 'ALBFaultRateAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-ALBFaultRateAlarm`,
      metric: new MathExpression({
        expression: 'faultCount / requestCount * 100',
        usingMetrics: {
          faultCount: albFaultCountMetric,
          requestCount: albRequestCountMetric,
        },
        period: this.defaultAlarmPeriod,
        label: 'Fault Rate',
      }),
      threshold: 0.1,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
  }

  createALBTargetFaultRateAlarm(): IAlarm {
    const albTargetFaultCountMetric = this.createALBTargetFaultCountMetric();
    const albRequestCountMetric = this.createTargetGroupRequestCountMetric();
    return new Alarm(this, 'ALBTargetFaultRateAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-ALBTargetFaultRateAlarm`,
      metric: new MathExpression({
        expression: 'faultCount / requestCount * 100',
        usingMetrics: {
          faultCount: albTargetFaultCountMetric,
          requestCount: albRequestCountMetric,
        },
        period: this.defaultAlarmPeriod,
        label: 'Fault Rate',
      }),
      threshold: 0.5,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
  }

  createApplicationFaultCountAlarm(operation: Operation): IAlarm {
    const faultCountMetric = this.createApplicationFaultMetric(operation, Stats.SUM);
    return new Alarm(this, `${operation.name}FaultCountAlarm`, {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-${operation.name}FaultCountAlarm`,
      metric: faultCountMetric,
      threshold: 1,
      evaluationPeriods: 1,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
  }

  createApplicationFaultRateAlarm(operation: Operation): IAlarm {
    return new Alarm(this, `${operation.name}FaultRateAlarm`, {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-${operation.name}FaultRateAlarm`,
      metric: this.createApplicationFaultMetric(operation, Stats.AVERAGE),
      threshold: 0.005,
      evaluationPeriods: 3,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
  }

  createApplicationLatencyAlarm(operation: Operation): IAlarm {
    const latencyMetric = this.createApplicationLatencyMetric(operation);
    return new Alarm(this, `${operation.name}LatencyAlarm`, {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-${operation.name}LatencyAlarm`,
      metric: latencyMetric,
      threshold: operation.latencyAlarmThreshold,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.NOT_BREACHING,
    });
  }

  createHeapMemoryAlarm(): IAlarm {
    const heapMemoryMetric = this.createHeapMemoryMetric();
    return new Alarm(this, 'HeapMemoryAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-HeapMemoryAlarm`,
      metric: heapMemoryMetric,
      threshold: 75,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createHeapMemoryHighSeverityAlarm(): IAlarm {
    const heapMemoryMetric = this.createHeapMemoryMetric();
    return new Alarm(this, 'HeapMemoryHighSeverityAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-HeapMemoryHighSeverityAlarm`,
      metric: heapMemoryMetric,
      threshold: 90,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createCPUUtilizationAlarm(): IAlarm {
    const cpuUtilizationMetric = this.createCPUUtilizationMetric();
    return new Alarm(this, 'CPUUtilizationAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-CPUUtilizationAlarm`,
      metric: cpuUtilizationMetric,
      threshold: 60,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createMemoryUtilizationAlarm(): IAlarm {
    const memoryUtilizationMetric = this.createMemoryUtilizationMetric();
    return new Alarm(this, 'MemoryUtilizationAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-MemoryUtilizationAlarm`,
      metric: memoryUtilizationMetric,
      threshold: 60,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createTrafficIntensityAlarm(): IAlarm {
    return new Alarm(this, 'TrafficIntensityHighSeverityAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-TrafficIntensityHighSeverityAlarm`,
      metric: this.createTrafficIntensityMetric(),
      threshold: 0.8,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createNoFileAlarm(): IAlarm {
    const noFileMetric = this.createNoFileMetric();
    return new Alarm(this, 'NoFileAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-NoFileAlarm`,
      metric: noFileMetric,
      threshold: 50,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createNoFileHighSeverityAlarm(): IAlarm {
    const noFileMetric = this.createNoFileMetric();
    return new Alarm(this, 'NoFileHighSeverityAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-NoFileHighSeverityAlarm`,
      metric: noFileMetric,
      threshold: 80,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createDiskUtilizationAlarm(): IAlarm {
    const diskUtilizationMetric = this.createDiskUtilizationMetric();
    return new Alarm(this, 'DiskUtilizationAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-DiskUtilizationAlarm`,
      metric: diskUtilizationMetric,
      threshold: 80,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createDiskUtilizationHighSeverityAlarm(): IAlarm {
    const diskUtilizationMetric = this.createDiskUtilizationMetric();
    return new Alarm(this, 'DiskUtilizationHighSeverityAlarm', {
      alarmName: `${this.serviceNamespace}-${this.props.stageName}-DiskUtilizationHighSeverityAlarm`,
      metric: diskUtilizationMetric,
      threshold: 90,
      evaluationPeriods: 5,
      comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createALBErrorCountMetric(): IMetric {
    return new Metric({
      namespace: 'AWS/ApplicationELB',
      metricName: 'HTTPCode_ELB_4XX_Count',
      dimensionsMap: {
        LoadBalancer: this.props.loadBalancer.loadBalancerFullName,
      },
      statistic: Stats.SUM,
      period: this.defaultAlarmPeriod,
      label: `ALBErrorCount${this.suffix}`,
    });
  }

  createALBFaultCountMetric(): IMetric {
    return new MathExpression({
      expression: 'httpCodeElb500 + httpCodeElb502 + httpCodeElb503 + httpCodeElb504',
      usingMetrics: {
        httpCodeElb500: this.props.loadBalancer.metrics.custom('HTTPCode_ELB_500_Count', {
          statistic: Stats.SUM,
          period: this.defaultAlarmPeriod,
        }),
        httpCodeElb502: this.props.loadBalancer.metrics.custom('HTTPCode_ELB_502_Count', {
          statistic: Stats.SUM,
          period: this.defaultAlarmPeriod,
        }),
        httpCodeElb503: this.props.loadBalancer.metrics.custom('HTTPCode_ELB_503_Count', {
          statistic: Stats.SUM,
          period: this.defaultAlarmPeriod,
        }),
        httpCodeElb504: this.props.loadBalancer.metrics.custom('HTTPCode_ELB_504_Count', {
          statistic: Stats.SUM,
          period: this.defaultAlarmPeriod,
        }),
      },
      period: this.defaultAlarmPeriod,
    });
  }

  createALBRequestCountMetric(): IMetric {
    return new Metric({
      namespace: 'AWS/ApplicationELB',
      metricName: 'RequestCount',
      dimensionsMap: {
        LoadBalancer: this.props.loadBalancer.loadBalancerFullName,
      },
      statistic: Stats.SUM,
      period: this.defaultAlarmPeriod,
      label: `ALBRequestCount${this.suffix}`,
    });
  }

  createTargetGroupRequestCountMetric(): IMetric {
    return new Metric({
      namespace: 'AWS/ApplicationELB',
      metricName: 'RequestCount',
      dimensionsMap: {
        LoadBalancer: this.props.loadBalancer.loadBalancerFullName,
        TargetGroup: this.props.targetGroup.targetGroupFullName,
      },
      statistic: Stats.SUM,
      period: this.defaultAlarmPeriod,
      label: `TargetGroupRequestCount${this.suffix}`,
    });
  }

  createALBTargetFaultCountMetric(): IMetric {
    return new Metric({
      namespace: 'AWS/ApplicationELB',
      metricName: 'HTTPCode_Target_5XX_Count',
      dimensionsMap: {
        LoadBalancer: this.props.loadBalancer.loadBalancerFullName,
        TargetGroup: this.props.targetGroup.targetGroupFullName,
      },
      statistic: Stats.SUM,
      period: this.defaultAlarmPeriod,
      label: `ALBTargetFaultCount${this.suffix}`,
    });
  }

  createApplicationFaultMetric(operation: Operation, statistic: string): IMetric {
    return new Metric({
      namespace: this.serviceNamespace,
      metricName: 'Fault',
      dimensionsMap: {
        MethodName: operation.name,
        MetricClass: 'NONE',
        Instance: 'NONE',
      },
      statistic,
      period: this.defaultAlarmPeriod,
      label: `${operation.name} Fault-${statistic}${this.suffix}`,
    });
  }

  createApplicationLatencyMetric(operation: Operation): IMetric {
    return new Metric({
      namespace: this.serviceNamespace,
      metricName: 'TotalTime',
      dimensionsMap: {
        MethodName: operation.name,
        MetricClass: 'NONE',
        Instance: 'NONE',
      },
      // Why use TrimmedMean for latency? https://w.amazon.com/bin/view/TrimmedMean
      statistic: Stats.trimmedMean(99),
      period: this.defaultAlarmPeriod,
      label: `${operation.name} Latency${this.suffix}`,
    });
  }

  createHeapMemoryMetric(): IMetric {
    return new Metric({
      namespace: this.serviceNamespace,
      metricName: 'HeapMemoryAfterGCUse',
      dimensionsMap: {
        MethodName: 'JMX',
      },
      statistic: Stats.MAXIMUM,
      period: this.defaultAlarmPeriod,
      label: `HeapMemory${this.suffix}`,
    });
  }

  createCPUUtilizationMetric(): IMetric {
    return new Metric({
      namespace: 'AWS/ECS',
      metricName: 'CPUUtilization',
      dimensionsMap: {
        ClusterName: this.props.service.cluster.clusterName,
        ServiceName: this.props.service.serviceName,
      },
      statistic: Stats.AVERAGE,
      period: this.defaultAlarmPeriod,
      label: `CPUUtilization${this.suffix}`,
    });
  }

  createMemoryUtilizationMetric(): IMetric {
    return new Metric({
      namespace: 'AWS/ECS',
      metricName: 'MemoryUtilization',
      dimensionsMap: {
        ClusterName: this.props.service.cluster.clusterName,
        ServiceName: this.props.service.serviceName,
      },
      statistic: Stats.AVERAGE,
      period: this.defaultAlarmPeriod,
      label: `MemoryUtilization${this.suffix}`,
    });
  }

  createTrafficIntensityMetric(): IMetric {
    return new Metric({
      namespace: this.serviceNamespace,
      metricName: 'TrafficIntensity',
      dimensionsMap: {
        MethodName: 'CoralPeriodicMetrics',
      },
      statistic: Stats.MAXIMUM,
      period: this.defaultAlarmPeriod,
      label: `TrafficIntensity${this.suffix}`,
    });
  }

  createNoFileMetric(): IMetric {
    return new Metric({
      namespace: this.serviceNamespace,
      metricName: 'FileDescriptorUse',
      dimensionsMap: {
        MethodName: 'JMX',
      },
      statistic: Stats.MAXIMUM,
      period: this.defaultAlarmPeriod,
      label: `NoFile${this.suffix}`,
    });
  }

  createDiskUtilizationMetric(): IMetric {
    return new Metric({
      namespace: this.serviceNamespace,
      metricName: 'DiskUse',
      dimensionsMap: {
        MethodName: 'JMX',
      },
      statistic: Stats.MAXIMUM,
      period: this.defaultAlarmPeriod,
      label: `DiskUtilization${this.suffix}`,
    });
  }

  createMissingLogDataAlarm(metricProps: LogGroupMetricProps): IAlarm {
    const logGroupDataMetric = this.createLogGroupDataMetric(metricProps);

    return new Alarm(this, `${metricProps.logGroupName}-MissingDataAlarm`, {
      alarmName: `${metricProps.logGroupName}-MissingDataAlarm`,
      alarmDescription: `When in alarm, ${metricProps.logGroupName} was expected to have new data but didn't.`,
      evaluationPeriods: 1,
      metric: logGroupDataMetric,
      threshold: 0,
      comparisonOperator: ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      treatMissingData: TreatMissingData.BREACHING,
    });
  }

  createLogGroupDataMetric(metricProps: LogGroupMetricProps): IMetric {
    const incomingLogEventsMetric = new Metric({
      namespace: 'AWS/Logs',
      metricName: 'IncomingLogEvents',
      dimensionsMap: {
        LogGroupName: metricProps.logGroupName,
      },
      period: metricProps.period ?? this.defaultAlarmPeriod,
      statistic: Stats.SUM,
    });

    if (!metricProps.dataOnlyWhenRequests) {
      return incomingLogEventsMetric;
    }

    const targetGroupRequestsMetric = new Metric({
      namespace: 'AWS/ApplicationELB',
      metricName: 'RequestCountPerTarget',
      statistic: Stats.SUM,
      dimensionsMap: {
        TargetGroup: this.props.targetGroup.targetGroupFullName,
      },
      label: 'Request Count',
      period: metricProps.period ?? this.defaultAlarmPeriod,
    });

    return new MathExpression({
      // format: IF(condition, trueValue, falseValue)
      // Evaluates to 1 if service is not receiving requests (targetGroupRequests = 0)
      // In alarm when service has requests and log group does not have incoming events
      expression: 'IF(targetGroupRequests > 0 AND FILL(logGroupIncomingLogEvents, 0) == 0, 0, 1)',
      period: metricProps.period ?? this.defaultAlarmPeriod,
      usingMetrics: {
        logGroupIncomingLogEvents: incomingLogEventsMetric,
        targetGroupRequests: targetGroupRequestsMetric,
      },
      label: 'Log events emitted when there were requests',
    });
  }

  createAlarms(): Alarming {
    const loadBalancerErrorRateAlarm = this.createALBErrorRateAlarm();
    const loadBalancerFaultRateAlarm = this.createALBFaultRateAlarm();
    const loadBalancerTargetFaultRateAlarm = this.createALBTargetFaultRateAlarm();
    const memoryUtilizationAlarm = this.createMemoryUtilizationAlarm();
    const cpuUtilizationAlarm = this.createCPUUtilizationAlarm();
    const heapMemoryAlarm = this.createHeapMemoryAlarm();
    const heapMemoryHighSeverityAlarm = this.createHeapMemoryHighSeverityAlarm();
    const trafficIntensityAlarm = this.createTrafficIntensityAlarm();
    const noFileAlarm = this.createNoFileAlarm();
    const noFileHighSeverityAlarm = this.createNoFileHighSeverityAlarm();
    const diskUtilizationAlarm = this.createDiskUtilizationAlarm();
    const diskUtilizationHighSeverityAlarm = this.createDiskUtilizationHighSeverityAlarm();

    const logGroupAlarms: IAlarm[] = this.props.logGroups.map((metricProps) => {
      return this.createMissingLogDataAlarm(metricProps);
    });

    return {
      loadBalancerErrorRateAlarm,
      loadBalancerFaultRateAlarm,
      loadBalancerTargetFaultRateAlarm,
      memoryUtilizationAlarm,
      cpuUtilizationAlarm,
      heapMemoryAlarm,
      heapMemoryHighSeverityAlarm,
      trafficIntensityAlarm,
      noFileAlarm,
      noFileHighSeverityAlarm,
      diskUtilizationAlarm,
      diskUtilizationHighSeverityAlarm,
      logGroupAlarms,
    };
  }

  /**
   * Creates CloudWatch Logs Insights query definitions for the service.
   * These queries are saved in CloudWatch Logs Insights and provide pre-configured
   * queries for common troubleshooting and monitoring scenarios.
   */
  private createCloudWatchLogsInsightsQueries(): void {
    const logGroupNames = this.props.logGroups.map((lg) => lg.logGroupName);

    // Trace Request by RequestID
    new CfnQueryDefinition(this, 'TraceRequestByIdQuery', {
      name: `${this.serviceNamespace}/TraceRequestById`,
      queryString: [
        '# Replace YOUR_REQUEST_ID with the actual requestId to trace',
        'fields @timestamp, @message, requestId, logger, level, @logStream',
        '| filter RequestId = "YOUR_REQUEST_ID" or requestId = "YOUR_REQUEST_ID"',
        '| sort @timestamp asc',
        '| limit 1000',
      ].join('\n'),
      logGroupNames,
    });

    // Recent Errors with RequestID
    new CfnQueryDefinition(this, 'RecentErrorsQuery', {
      name: `${this.serviceNamespace}/RecentErrors`,
      queryString: [
        'fields @timestamp, requestId, logger, message, exception, @logStream',
        '| filter level = "ERROR"',
        '| sort @timestamp desc',
        '| limit 100',
      ].join('\n'),
      logGroupNames,
    });

    // Slow Requests
    new CfnQueryDefinition(this, 'SlowRequestsQuery', {
      name: `${this.serviceNamespace}/SlowRequests`,
      queryString: [
        '# Replace YOUR_THRESHOLD_MS with the desired latency threshold in milliseconds (e.g., 200, 500, 1000)',
        'fields @timestamp, MethodName, TotalTime, requestId, @logStream',
        '| filter MethodName not in ["JMX", "CoralPeriodicMetrics", "BSFPing"]',
        '| filter TotalTime > YOUR_THRESHOLD_MS',
        '| sort TotalTime desc',
        '| limit 100',
      ].join('\n'),
      logGroupNames,
    });

    // Error Frequency Analysis
    new CfnQueryDefinition(this, 'ErrorFrequencyQuery', {
      name: `${this.serviceNamespace}/ErrorFrequency`,
      queryString: [
        'fields message',
        '| filter level = "ERROR" and !isblank(message)',
        '| stats count(message) as errorCount by message',
        '| sort errorCount desc',
        '| limit 20',
      ].join('\n'),
      logGroupNames,
    });

    // Request Volume by Operation
    new CfnQueryDefinition(this, 'RequestVolumeQuery', {
      name: `${this.serviceNamespace}/RequestVolumeByOperation`,
      queryString: [
        'fields @timestamp, MethodName',
        '| filter ispresent(MethodName) and MethodName not in ["JMX", "CoralPeriodicMetrics"]',
        '| stats count() as requests by MethodName, bin(5m)',
        '| sort @timestamp asc',
      ].join('\n'),
      logGroupNames,
    });

    // Operation Latency Percentiles
    new CfnQueryDefinition(this, 'LatencyPercentilesQuery', {
      name: `${this.serviceNamespace}/LatencyPercentiles`,
      queryString: [
        'fields TotalTime, MethodName',
        '| filter ispresent(MethodName) and MethodName not in ["JMX", "CoralPeriodicMetrics", "BSFPing"]',
        '| stats avg(TotalTime) as avg,',
        '        pct(TotalTime, 50) as p50,',
        '        pct(TotalTime, 90) as p90,',
        '        pct(TotalTime, 99) as p99,',
        '        max(TotalTime) as max,',
        '        count(*) as requests',
        '        by MethodName',
        '| sort p90 desc',
      ].join('\n'),
      logGroupNames,
    });

    // Error Rate Over Time
    new CfnQueryDefinition(this, 'ErrorRateOverTimeQuery', {
      name: `${this.serviceNamespace}/ErrorRateOverTime`,
      queryString: [
        'fields @timestamp, level',
        '| filter level = "ERROR"',
        '| stats count() as errorCount by bin(5m)',
        '| sort @timestamp asc',
      ].join('\n'),
      logGroupNames,
    });
  }
}
