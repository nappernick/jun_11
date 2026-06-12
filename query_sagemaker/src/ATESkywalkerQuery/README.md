# Getting Started

This package contains the source code for your application.

## How The Dockerfile Entrypoint Command Works

The script we run to start your application (`entry_point.sh`) is in this package's configuration/bin/ directory, and you are free to replace it with anything else you'd like.  However, there are some benefits to understanding the system we've provided and working with it rather than discarding it.

By default, `entry_point.sh` invokes [a script provided by the ApolloShimOpConfigHelpers package](https://code.amazon.com/packages/ApolloShimOpConfigHelpers/blobs/88f29d0d13fcbb6a207b0ec1e8ac90038883b623/--/bin/bones_run_apollo_shim.sh#L9), `bones_run_apollo_shim.sh`.  This script performs all the actions required to set up your Apollo-like directory structure inside the Docker container, runs ApolloCmd scripts, etc.

Notice that in the `entry_point.sh` script, we pass an argument to the `bones_run_apollo_shim.sh`:

```sh
exec /opt/amazon/bin/bones_run_apollo_shim.sh --script bin/run-service.sh
```

 This tells the Apollo Shim setup scripts that after they finish setting up your fake Apollo environment, they should run a script located in the Apollo Environment Root called "bin/run-service.sh".  This is the script that actually launches the Coral Stack inside the Docker container, and is currently being provided by [the Cloud9ApolloJavaWrapperGenerator package](https://code.amazon.com/packages/Cloud9JavaWrapperGenerator), which this package declares a dependency on.

Altogether, what this means is that you have some choices here.

1. If you want to do your own thing and operate with as light a footprint as possible, you can skip creating a bunch of junk in your Docker container by updating your "entry_point.sh" script not to call the Apollo Shim Setup scripts.
2. If you want to provide your own custom startup script while still having all of the Apollo Shim Setup stuff executed, you just need to update your "entry_point.sh" script to pass a different script into the Apollo Shim Setup scripts and make sure that script ends up in the runtime-closure of your application

## Troubleshooting Issues

### Docker Fundamentals

If you haven't played around with Docker before, we recommend you take a look at the Docker documentation.  Specific articles you should take a look at:

* [The Docker introductory tutorial](https://docs.docker.com/get-started/)
* [The Docker best practices guide](https://docs.docker.com/develop/dev-best-practices/)
* [The Dockerfile reference](https://docs.docker.com/engine/reference/builder/)

### Application Spinup Issues

If you've never worked with Docker before, it can be mysterious how your application starts up inside the container and therefore difficult to figure out why it's failing to start up.  To explain this, we first need to go over how [a Docker build](https://docs.docker.com/engine/reference/commandline/build/) works.

Your [Dockerfile specifies a series of commands](https://docs.docker.com/engine/reference/builder/) that the Docker build will run in order to construct your Docker image.  The first command that runs is usually the `FROM` command, which specifies the base image on top of which to add your own changes (Amazon Linux, by default).  Every other command is run as if it were the "root" user inside of that image.  By default, the image will only contain the files defined by your base image.  If you want stuff from your runtime closure to show up in it, you'll need to use [the Dockerfile COPY command](https://docs.docker.com/engine/reference/builder/#copy).

The final command in your Dockerfile is typically going to be the entrypoint to your application.  That is, it will be the command that Docker runs to start up your application.  Your Docker container will stay alive as long as the process that the entrypoint command runs is alive, and the Docker container will spin down when that process exits.  Logs emitted by that process to stdout/stderr should show up in your terminal window, and you can use these messages to troubleshoot why your application is failing to spin up.  Logs emitted to files on "disk" inside your container do not get forwarded to the Docker logging facility by default (you can [read more about logging with Docker here](https://docs.docker.com/config/containers/logging/)).  It is [a Docker best practice to for each container to have a single running process](https://docs.docker.com/develop/develop-images/dockerfile_best-practices/#decouple-applications) so you should generally consider it pathological to have your application configured to expect some other process to take logs you've written to disk and send them elsewhere (Timber, CloudWatch Logs, etc).

# JDK Version

This application is built using JDK 25 by default, which is the recommended version by both the JDK and Coral team.
To use another JDK version (such as JDK 17), follow these steps:
1. Replace `JDK25` with `JDK17` in Config of all Java packages
2. Configure Hydra runtime to `java17`
