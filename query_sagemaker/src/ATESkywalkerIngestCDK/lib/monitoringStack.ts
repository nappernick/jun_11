import { DeploymentEnvironment, DeploymentStack, SoftwareType } from '@amzn/pipelines';
import { Duration } from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  Alarm,
  AlarmWidget,
  ComparisonOperator,
  Dashboard,
  GraphWidget,
  MathExpression,
  PeriodOverride,
  TextWidget,
  TreatMissingData,
} from 'aws-cdk-lib/aws-cloudwatch';
import { IFunction } from 'aws-cdk-lib/aws-lambda';

interface MonitoringStackProps {
  readonly env: DeploymentEnvironment;
  readonly pollerLambda: IFunction;
  readonly processorLambda: IFunction;
}

export class MonitoringStack extends DeploymentStack {
  constructor(scope: Construct, id: string, readonly props: MonitoringStackProps) {
    super(scope, id, {
      env: props.env,
      softwareType: SoftwareType.INFRASTRUCTURE,
    });
    this.createSummaryDashboard();
    this.createServiceDashboard();
  }

  private createSummaryDashboard() {
    const summaryDashboard = new Dashboard(this, 'SummaryDashboard', {
      dashboardName: 'ATESkywalkerIngest-Summary',
      start: '-' + Duration.days(14).toIsoString(),
      periodOverride: PeriodOverride.INHERIT,
    });

    summaryDashboard.addWidgets(new TextWidget({ width: 24, height: 1, markdown: '# Summary dashboard' }));

    for (const [name, fn] of this.lambdaEntries()) {
      summaryDashboard.addWidgets(
        new TextWidget({ width: 24, height: 1, markdown: `## ${name}` }),
        new GraphWidget({
          width: 24,
          height: 6,
          title: `${name} — AVG TPS (1 minute)`,
          left: [
            new MathExpression({
              expression: 'requests/PERIOD(requests)',
              usingMetrics: { requests: fn.metricInvocations() },
              label: 'TPS',
              period: Duration.minutes(1),
            }),
          ],
          leftYAxis: { min: 0, showUnits: false },
        }),
        new GraphWidget({
          width: 24,
          height: 6,
          title: `${name} — Duration`,
          left: [
            fn.metricDuration({ statistic: 'p50', label: 'P50' }),
            fn.metricDuration({ statistic: 'p90', label: 'P90' }),
            fn.metricDuration({ statistic: 'p99', label: 'P99' }),
          ],
          leftYAxis: { min: 0, label: 'ms', showUnits: false },
        }),
        new GraphWidget({
          width: 24,
          height: 6,
          title: `${name} — Error`,
          left: [fn.metricErrors({ label: 'Counts' })],
          right: [
            new MathExpression({
              expression: 'errors*100/invocations',
              usingMetrics: {
                errors: fn.metricErrors(),
                invocations: fn.metricInvocations(),
              },
              label: 'Rates',
            }),
          ],
          leftYAxis: { min: 0, showUnits: false },
          rightYAxis: { min: 0, label: '%', showUnits: false },
        }),
      );
    }
  }

  private createServiceDashboard() {
    const serviceDashboard = new Dashboard(this, 'ServiceDashboard', {
      dashboardName: 'ATESkywalkerIngest-Service',
      start: '-' + Duration.hours(8).toIsoString(),
      periodOverride: PeriodOverride.INHERIT,
    });

    serviceDashboard.addWidgets(new TextWidget({ width: 24, height: 1, markdown: '# Service dashboard' }));

    for (const [name, fn] of this.lambdaEntries()) {
      serviceDashboard.addWidgets(
        new TextWidget({ width: 24, height: 1, markdown: `## ${name}` }),
        new AlarmWidget({
          width: 12,
          height: 6,
          title: `${name} — Error count`,
          alarm: new Alarm(this, `${name}ErrorCountAlarm`, {
            metric: fn.metricErrors({ period: Duration.minutes(1) }),
            threshold: 1,
            comparisonOperator: ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluationPeriods: 3,
            datapointsToAlarm: 2,
            treatMissingData: TreatMissingData.NOT_BREACHING,
          }),
          leftYAxis: { min: 0, showUnits: false },
        }),
        new AlarmWidget({
          width: 12,
          height: 6,
          title: `${name} — Error rate`,
          alarm: new Alarm(this, `${name}ErrorRateAlarm`, {
            metric: new MathExpression({
              expression: 'errors*100/invocations',
              usingMetrics: {
                errors: fn.metricErrors(),
                invocations: fn.metricInvocations(),
              },
              period: Duration.minutes(1),
              label: 'Rates',
            }),
            threshold: 0.5,
            evaluationPeriods: 5,
            treatMissingData: TreatMissingData.NOT_BREACHING,
          }),
          leftYAxis: { min: 0, label: '%', showUnits: false },
        }),
        new AlarmWidget({
          width: 24,
          height: 6,
          title: `${name} — Throttle rate`,
          alarm: new Alarm(this, `${name}ThrottleRateAlarm`, {
            metric: new MathExpression({
              expression: 'throttles*100/invocations',
              usingMetrics: {
                throttles: fn.metricThrottles(),
                invocations: fn.metricInvocations(),
              },
              period: Duration.minutes(1),
              label: 'Rates',
            }),
            threshold: 1,
            evaluationPeriods: 5,
            treatMissingData: TreatMissingData.NOT_BREACHING,
          }),
          leftYAxis: { min: 0, label: '%', showUnits: false },
        }),
        new AlarmWidget({
          width: 24,
          height: 6,
          title: `${name} — Duration`,
          alarm: new Alarm(this, `${name}DurationAlarm`, {
            metric: fn.metricDuration({
              statistic: 'p99',
              label: 'P99',
              period: Duration.minutes(1),
            }),
            threshold: 5000,
            evaluationPeriods: 5,
            treatMissingData: TreatMissingData.NOT_BREACHING,
          }),
          leftYAxis: { min: 0, label: 'ms', showUnits: false },
        }),
      );
    }
  }

  private lambdaEntries(): [string, IFunction][] {
    return [
      ['Poller', this.props.pollerLambda],
      ['Processor', this.props.processorLambda],
    ];
  }
}
