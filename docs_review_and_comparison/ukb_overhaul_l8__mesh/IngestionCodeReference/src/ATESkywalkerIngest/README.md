## Overview

This package is an example Java Lambda package for using with CDK Pipeline. It doesn't have an API Gateway definition
associated with it. It's most useful when you just want to deploy a lambda function, perhaps for use as a stream consumer,
or invoked by SQS, SNS, or CloudWatch.

In a nutshell, it's just a normal Java package with additional configurations for BATS transformation. HappierTrails
copies the file `configuration/aws_lambda/lambda-transform.yml` to the build output. BATS requires this file to know how
to transform this package into a deployable Lambda package. If you customize your build system, don't forget this
requirement. Read more about [BATS Lambda Transformer here](https://builderhub.corp.amazon.com/docs/bats/user-guide/transformers-lambda.html).

This package does not contain any deployment logic, they are defined in CDK Package.

## Integrating with existing CDK package

If your CDK package does not have any stages or stacks yet, follow our guides to
[add new resources](https://builderhub.corp.amazon.com/docs/native-aws/developer-guide/cdk-howto-add-resources.html) and [stage/region](https://builderhub.corp.amazon.com/docs/native-aws/developer-guide/cdk-howto-expand-pipeline.html) to your application.

Once you have your stack ready, add the sample Lambda function using this snippet:

```
  new lambda.Function(this, 'Calculator', {
    code: LambdaAsset.fromBrazil({
      brazilPackage: BrazilPackage.fromString('ATESkywalkerIngest-1.0'),
      componentName: 'Calculator',
    }),
    handler: 'com.amazon.ateskywalkeringest.lambda.calculator.Calculator::add',
    memorySize: 512,
    timeout: cdk.Duration.seconds(30),
    runtime: lambda.Runtime.JAVA_17
  });
```

## General Workflow

For testing with this Lambda package, here's our current recommendation:

1. Unit tests. Run good old-fashioned unit tests against your code.
1. Deploy to your personal stack and validate the functionalities there. This needs to be done in two steps:
   1. Run `brazil-build` in this package.
   1. Run `brazil-build cdk deploy --hotswap $StackName` in your CDK package.
1. CR and Push. Run integration tests in your pipeline for your function.

## Using SAM to run and test locally

The [AWS SAM CLI](https://github.com/aws/aws-sam-cli) is an open-source CLI tool
that helps you test and debug Lambda functions locally. If you don’t already have it,
[go here to install it](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html). To use the SAM CLI with this Lambda package, first make sure
the CDK package that defines it is also in your workspace. Then, go to the root of the
package where your Lambda function is defined:

```bash
cd ~/workplace/ATESkywalkerIngest/src/ATESkywalkerIngest/
```

Build the runtime with SAM:

```bash
sam build -t ../ATESkywalkerIngestCDK/build/cdk.out/ATESkywalkerIngest-Service-alpha.template.json
```

The output of that command should look like the below:

```bash
Building codeuri: /local/home/{Username}/workplace/ATESkywalkerIngest/src/ATESkywalkerIngestCDK/build/cdk.out runtime: java17 metadata: {'aws:cdk:path': 'ATESkywalkerIngest-Service-alpha/ATESkywalkerIngest/Resource', 'aws:asset:path': '.', 'BuildMethod': 'makefile', 'aws:asset:property': 'Code'} architecture: x86_64 functions: ATESkywalkerIngest
Running CustomMakeBuilder:CopySource
Running CustomMakeBuilder:MakeBuild
Current Artifacts Directory : /local/home/{Username}/workplace/ATESkywalkerIngest/src/ATESkywalkerIngest/.aws-sam/build/ATESkywalkerIngest3F172940

...

Build Succeeded

Built Artifacts  : .aws-sam/build
Built Template   : .aws-sam/build/template.yaml

Commands you can use next
=========================
[*] Validate SAM template: sam validate
[*] Invoke Function: sam local invoke
[*] Test Function in the Cloud: sam sync --stack-name {stack-name} --watch
[*] Deploy: sam deploy --guided
```

Next, create a new directory to store the sample event content:

```bash
mkdir -p sam/events
```

Then create a file `sam/events/sample_event.json` with a test event you want to invoke the Lambda function with:

```json
{ "x": 1, "y": 2 }
```

Invoke the Lambda function with the sample event:

```bash
sam local invoke ATESkywalkerIngest -e sam/events/sample_event.json
```

The output of that command should contain the results of the function’s execution:

```bash
Invoking com.amazon.ateskywalkeringest.lambda.calculator.Calculator::add (java17)

...

{"statusCode":"200","body":"3.0"}END RequestId: 621a66b8-6bd4-4045-b681-1ffba907a53c
REPORT RequestId: 621a66b8-6bd4-4045-b681-1ffba907a53c  Init Duration: 0.13 ms  Duration: 12
```
