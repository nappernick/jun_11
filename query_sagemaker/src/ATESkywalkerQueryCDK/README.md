## CDK Infrastructure

### Introduction

This package contains the Cloud Development Kit (CDK) classes that define all the Native AWS Resources associated with your
application. These classes are written in TypeScript, a statically-bound language based off of JavaScript. The build system
is Node, by way of an internal Amazon wrapper called "NpmPrettyMuch".

When this package is built, it produces a set of CloudFormation templates in the `./build/cdk.out` directory, one for each
of the CloudFormation stacks the package defines in its CDK App. You can deploy these CloudFormation templates to your AWS
Account using the CDK Toolkit, which is included in this package, as explained below.

Helpful links if you're just getting started:

- [`PipelineUpdate` target](https://docs.hub.amazon.dev/pipelines/cdk-guide/concepts-managing-pipeline-via-code/)
- [AWS Docs on CDK](https://docs.aws.amazon.com/cdk/latest/guide/what-is.html)
- [Wiki page for NpmPrettyMuch](https://w.amazon.com/index.php/NpmPrettyMuch)
- [Github page for the CDK](https://github.com/awslabs/aws-cdk)
- [CDK TypeScript reference](https://docs.aws.amazon.com/cdk/api/latest/typescript/api/index.html)

### The AWS Resources this CDK App defines

This CDK App spins up 6 CloudFormation stacks when deployed to your developer account, plus a few for
bootstrapping/resources needed by Builder Tools systems. We often call these extra stacks "scaffolding" or "bootstrap"
stacks. The terms are used interchangably.

1. **A VPC Stack**. This includes up to three subnets (depending on how you configure it and the region you deploy to) as
   well as all the networking components to make your ECS Service accessible both within and without the VPC.
1. **An Application Definition Stack**. This stack contains resources which define key aspects of your application. This will Create your Superstar environment, link you to the CloudAuth VPC,
   create a SOA service for authentication and connectivity, and generate DNS entries which will front your service endpoints.
1. **An ECS Cluster Stack**. This contains just a single ECS/Fargate Cluster. It's a separate stack to make it easier for
   you to share it across multiple ECS/Fargate Services and contains the Load Balancer. A second listener is created on the
   load balancer to allow the OnePod Hydra test to target the OnePod service.
1. **ECS OnePod and Fleet Service Stacks**. These contain your ECS Services and a collection of Metric Filters that provide
   feedback on the health of your service.
1. **ECS OnePod and Fleet Monitoring Stacks**. These contain alarms on the service's log groups to monitor whether logs are being written as expected.
1. **A pipeline stack**. This contains the shape of your pipeline and how the application stacks (above) are modeled in
   your pipeline.

### A quick tour of this package's contents

- `./lib/app.ts` - This is a TypeScript file in which we [define our CDK App](https://docs.aws.amazon.com/cdk/latest/guide/apps_and_stacks.html).
  We instantiate our CDK Stacks and associate them with our app, ensuring that when we run
  [the CDK Toolkit](https://docs.aws.amazon.com/cdk/latest/guide/tools.html) it generates CloudFormation templates for them
  and deploys them as appropriate. It creates an internal Pipeline using the
  [Pipelines CDK Constructs](https://code.amazon.com/packages/PipelinesConstructs/blobs/mainline/--/README.md) which automatically
  updates our CDK Stacks when changes are pushed to the `mainline` branch. By default we set `selfMutate` to `true` so the
  pipeline is self-updating.
- `./lib/foundational_resources.ts` - This is a TypeScript file in which we
  [define the CDK Stack](https://docs.aws.amazon.com/cdk/latest/guide/apps_and_stacks.html) for our Application Definition. If you want to
  add additional AWS resources to this CloudFormation stack, add them here.
- `./lib/approval_workflow.ts` - This is a TypeScript file in which we create the steps (e.g., Hydra integration tests) that
  are added to approval workflows in the pipeline.
- `./lib/ecs_cluster.ts` - This is a TypeScript file in which we
  [define the CDK Stack](https://docs.aws.amazon.com/cdk/latest/guide/apps_and_stacks.html) for our ECS Cluster. If you want to
  add additional AWS resources to this CloudFormation stack, add them here.
- `./lib/ecs_service.ts` - This is a TypeScript file in which we
  [define the CDK Stack](https://docs.aws.amazon.com/cdk/latest/guide/apps_and_stacks.html) for our ECS Service. If you want to
  add additional AWS resources to this CloudFormation stack, add them here.
- `./lib/monitoring.ts` - This is a TypeScript file in which we
  [define the CDK Stack](https://docs.aws.amazon.com/cdk/latest/guide/apps_and_stacks.html) that contains CloudWatch alarms to
  help monitor the health of the service. If you want to add additional AWS resources to this CloudFormation stack, add them here.
- `./lib/vpc.ts` - This is a TypeScript file in which we
  [define the CDK Stack](https://docs.aws.amazon.com/cdk/latest/guide/apps_and_stacks.html) for our VPC. If you want to add additional
  AWS resources to this CloudFormation stack, add them here.
- `./package.json` - This is [a standard Node Package file](https://docs.npmjs.com/files/package.json). It manages how this
  package is built, from what commands are run at each step of the build process to what the package's dependencies are.

  Side note: if you're unfamiliar with how Node handles dependencies, we recommend you [read this article](https://lexi-lambda.github.io/blog/2016/08/24/understanding-the-npm-dependency-model/). Node treats them differently than other common programming languages,
  with each package being able to have its own, fully independent set of dependencies instead of all packages in a dependency hierarchy
  being forced to resolve to a single, shared version of a dependency.

Most of your resources probably belong in your `./lib/ecs_service.ts` file - as these are service specific resources. You're
free to add additional resources as you see fit.

You can also refactor the `./lib/app.ts` file to be more factory based if you like - such that you just loop over the
regions/accounts/stages you want to have in your pipeline.
