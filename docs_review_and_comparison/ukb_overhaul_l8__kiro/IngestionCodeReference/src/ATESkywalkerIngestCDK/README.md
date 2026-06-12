## Welcome!

This package will help you manage Pipelines and your AWS Infrastructure with the power of CDK!

You can view this package's pipeline, [ATESkywalkerIngest](https://pipelines.amazon.com/pipelines/ATESkywalkerIngest)

## Development

```bash
brazil ws create --name ATESkywalkerIngest
cd ATESkywalkerIngest
brazil ws use \
  --versionset ATESkywalkerIngest/development \
  --package ATESkywalkerIngestCDK
cd src/ATESkywalkerIngestCDK
brazil-build
```

## Useful links:

- https://builderhub.corp.amazon.com/docs/native-aws/developer-guide/cdk-pipeline.html
- https://code.amazon.com/packages/PipelinesConstructs/blobs/mainline/--/README.md
- https://code.amazon.com/packages/CDKBuild/blobs/HEAD/--/README.md
- https://docs.aws.amazon.com/cdk/api/latest/versions.html
