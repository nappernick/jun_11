import { Match, Template } from 'aws-cdk-lib/assertions';
import { DeploymentEnvironmentFactory } from '@amzn/pipelines';
import { App } from 'aws-cdk-lib';
import { ServiceStack } from '../lib/serviceStack';

test('create expected Service Resources', () => {
  const mockApp = new App();
  const stack = new ServiceStack(mockApp, 'id', {
    stage: 'alpha',
    isProd: false,
    env: DeploymentEnvironmentFactory.fromAccountAndRegion('test-account', 'us-west-2', 'unique-id'),
    corexRoleArn: 'arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta',
    corexSessionPrefix: 'ATESkywalker',
  });
  const template = Template.fromStack(stack);

  template.resourceCountIs('AWS::Lambda::Function', 2);

  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'ATESkywalkerIngest-Poller-alpha',
    Handler: 'com.amazon.ingestion.lambda.poller.Poller::handleRequest',
    Environment: {
      Variables: {
        PROCESSOR_FUNCTION_NAME: Match.anyValue(),
        COREX_ROLE_ARN: 'arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta',
        COREX_ROLE_SESSION_PREFIX: 'ATESkywalker',
        COREX_SECRET_NAME: 'ATESkywalkerIngest/alpha/corex',
        SSM_LAST_POLL_TIMESTAMP: '/ATESkywalkerIngest/alpha/last-poll-timestamp',
        SSM_LAST_CONTENT_UPDATE_TIMESTAMP: '/ATESkywalkerIngest/alpha/last-content-update-timestamp',
      },
    },
  });

  template.hasResourceProperties('AWS::Lambda::Function', {
    FunctionName: 'ATESkywalkerIngest-Processor-alpha',
    Handler: 'com.amazon.ingestion.lambda.processor.Processor::handleRequest',
    Environment: {
      Variables: {
        COREX_ROLE_ARN: 'arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta',
        COREX_ROLE_SESSION_PREFIX: 'ATESkywalker',
        COREX_SECRET_NAME: 'ATESkywalkerIngest/alpha/corex',
      },
    },
  });

  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([
        Match.objectLike({
          Action: 'sts:AssumeRole',
          Effect: 'Allow',
          Resource: 'arn:aws:iam::975754358161:role/ATESkywalker-CORExApiRole-beta',
        }),
      ]),
    }),
  });

  template.hasResourceProperties('AWS::IAM::Policy', {
    PolicyDocument: Match.objectLike({
      Statement: Match.arrayWith([Match.objectLike({ Action: 'lambda:InvokeFunction', Effect: 'Allow' })]),
    }),
  });
});
