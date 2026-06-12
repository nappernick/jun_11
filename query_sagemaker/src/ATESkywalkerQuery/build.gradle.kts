// Applies core Gradle plugins, which are ones built into Gradle itself.
plugins {
    /*
     Java for compile and unit test of Java source files. Read more at:
     https://docs.gradle.org/current/userguide/java_plugin.html
    */
    java

    /*
     JaCoCo for coverage metrics and reports of Java source files. Read more at:
     https://docs.gradle.org/current/userguide/jacoco_plugin.html
    */
    jacoco

    /*
     Checkstyle for style checks and reports on Java source files. Read more at:
     https://docs.gradle.org/current/userguide/checkstyle_plugin.html
    */
    checkstyle

    id("brazil-gradle")
    id("brazil-gradle-java-presets")
    id("brazil-generate-wrapper") apply(false)
    id("brazil-validate-classpath")
    id("amazon-java-format")
    id("com.github.spotbugs")
}

brazilGradle {
    configureBasicDependencies()
    configureAnnotationProcessors()
}

tasks.withType<JavaCompile>().configureEach {
    options.compilerArgs.add("-Xlint:deprecation")
    options.compilerArgs.add("-Xlint:unchecked")
}

/*
  Configures the JaCoCo "JacocoCoverageVerification" plugin.
  The test coverage threshold shouldn't be set in the code.
  Instead, the current best practice is to use Coverlay,
  where coverage requirements can be configured at org level.
  https://docs.hub.amazon.dev/coverlay/
  Artificially failing a build (e.g. because of insufficient test coverage)
  blocks all progress globally.
  https://quip-amazon.com/JLVHAfPOW5tP/Artificial-Build-Breaks-Prevent-Maintenance
*/
tasks.withType<JacocoCoverageVerification>() {
    violationRules {
        rule {
            excludes = listOf (
                "*.module.*",
            )
            limit {
                minimum = BigDecimal.valueOf(0)
            }
        }
    }
}

/*
  Configures JaCoCo test report generation.
  This ensures that excluded classes don't appear in coverage reports.
*/
tasks.withType<JacocoReport>() {
    classDirectories.setFrom(
        files(classDirectories.files.map {
            fileTree(it) {
                exclude(
                    "**/module/**"
                )
            }
        })
    )
}

tasks.named("check").configure {
    dependsOn(tasks.jacocoTestCoverageVerification)
}

/*
 Configures the Checkstyle "checkstyle" plugin. Remove this and the plugin
 to skip these checks and report generation.
*/
tasks.withType<Checkstyle>() {
    setIgnoreFailures(false)
}

/*
 Configures the SpotBugs "com.github.spotbugs" plugin. Remove this and the
 plugin to skip these checks and report generation.
*/
tasks.withType<com.github.spotbugs.snom.SpotBugsTask>() {
    ignoreFailures = false
}

// Specifies that JUnit Platform (a.k.a. JUnit 5) should be used to execute tests.
tasks.withType<Test> {
    useJUnitPlatform()

    systemProperty("junit.jupiter.execution.parallel.enabled", "true")
    systemProperty("junit.jupiter.execution.parallel.mode.classes.default", "concurrent")
}

val staticContentFiles = copySpec {
    from("${brazilGradle.path("package-src-root")}/static-content")
}

val copyStaticContentToBuild = tasks.register<Copy>("copyStaticContentToBuild") {
    into("${brazilGradle.buildDir}/static-content")
    with(staticContentFiles)
}

tasks.named("coverageReportSummary").configure {
    dependsOn("copyConfigurationToBuild")
}

val copyStaticContentToBuildPrivate = tasks.register<Copy>("copyStaticContentToBuildPrivate") {
    into("${brazilGradle.buildDir}/private/static-content")
    with(staticContentFiles)
}

val configurationFiles = copySpec {
    from("${brazilGradle.path("package-src-root")}/configuration")
}

val copyConfigurationToBuild = tasks.register<Copy>("copyConfigurationToBuild") {
    into("${brazilGradle.buildDir}")
    with(configurationFiles)
}

val copyConfigurationToBuildPrivate = tasks.register<Copy>("copyConfigurationToBuildPrivate") {
    from(brazilGradle.path("run.configfarm.brazil-config")) {
        include("brazil-config/**/*")
    }
    from(brazilGradle.path("run.configfarm.certs")) {
        include("certs/**/*")
    }
    into("${brazilGradle.buildDir}/private")
    with(configurationFiles)

    project.mkdir("${brazilGradle.buildDir}/private/var/tmp")
}

/*
 Generate the Apollo script to start your service.
 Note: When modifying values here remember to also update the server target.
*/
val apolloScript = tasks.register<com.amazon.brazil.gradle.launcher.GenerateWrapperTask>("apolloScript") {
    target("${brazilGradle.buildDir}/bin/run-service.sh")
    main("com.amazon.ateskywalkerquery.ATESkywalkerQuery")

    jvmArgs("-XX:MaxRAMPercentage=75.0")
    jvmArgs("-XX:+PerfDisableSharedMem")
    // Kill on OOM (logscan for PMAdmin.log will trigger an alarm)
    jvmArgs("-XX:+ExitOnOutOfMemoryError")
    jvmArgs("-XX:+ErrorFileToStderr")

    environment("CORAL_CONFIG_PATH", "\${ENVROOT}/coral-config", false)

    systemProperty("javax.net.ssl.trustStore", "\${ENVROOT}/certs/InternalAndExternalTrustStore.jks", false)
    systemProperty("javax.net.ssl.trustStorePassword", "amazon")
    systemProperty("log4j.configurationFile", "file:\${ENVROOT}/log-configuration/log4j2-container.xml", false)
    systemProperty("java.util.logging.manager", "org.apache.logging.log4j.jul.LogManager")
    systemProperty("Log4jContextSelector", "org.apache.logging.log4j.core.async.AsyncLoggerContextSelector")
    // WARNING: See https://w.amazon.com/bin/view/Coral/Manual/AuthHandler before removing
    systemProperty("com.amazon.coral.blockNoAuthRequests", "true")
    systemProperty("root", "\${ENVROOT}", false)
    systemProperty("java.security.properties", "\${ENVROOT}/jre-config/java.security.override", false);

    args("--root=\${ENVROOT}", false)
    args("--domain=\${DOMAIN}", false)
    args("--realm=\${REALM}", false)
}

/*
 Launch the coral server.
 Note: When modifying values here remember to also update the apolloScript target if production-applicable.
*/
val server = tasks.register<JavaExec>("server") {
    dependsOn(copyStaticContentToBuildPrivate, copyConfigurationToBuildPrivate)
    classpath = project.sourceSets.main.get().runtimeClasspath
    mainClass.set("com.amazon.ateskywalkerquery.ATESkywalkerQuery")

    /*
     Set to true to enable remote debugging on port 5005 of your `bb server`.
     For more information, see https://docs.gradle.org/current/dsl/org.gradle.api.tasks.JavaExec.html#org.gradle.api.tasks.JavaExec:debug
    */
    debug = false

    environment("CORAL_CONFIG_PATH", brazilGradle.path("run.coralconfig"))
    environment("LD_LIBRARY_PATH", brazilGradle.path("run.lib"))
    environment("IS_ONEPOD", "false")
    environment("AWS_REGION", "us-west-2")
    environment("NAMESPACE", "ATESkywalkerQuery")

    systemProperty("java.util.logging.manager", "org.apache.logging.log4j.jul.LogManager")
    systemProperty("Log4jContextSelector", "org.apache.logging.log4j.core.async.AsyncLoggerContextSelector")
    systemProperty("log4j.configurationFile", "${brazilGradle.path("package-src-root")}/configuration/log-configuration/log4j2-local.xml")
    systemProperty("java.io.tmpdir", "${brazilGradle.buildDir}/private/var/tmp")
    systemProperty("java.net.preferIPv4Stack", "true")
    systemProperty("root","${brazilGradle.buildDir}/private")
    systemProperty("java.security.properties", "${brazilGradle.path("package-src-root")}/configuration/jre-config/java.security.override")

    // Only enable Coral Explorer for local development (i.e. bb server).
    systemProperty("coral.explorer.enable", "true")
    // Disable Cloudauth -- only do this when running on local machine
    systemProperty("cloudauth.disable", "true")

    jvmArgs("-XX:MaxRAMPercentage=75.0")
    jvmArgs("-XX:MaxGCPauseMillis=100")
    jvmArgs("-XX:+ErrorFileToStderr")
    jvmArgs("-Droot=${brazilGradle.buildDir}/private")

    args("--root=${brazilGradle.buildDir}/private")
    args("--domain=test")
    args("--realm=us-west-2")
}

tasks.named("build").configure {
    dependsOn(copyStaticContentToBuild, copyConfigurationToBuild, apolloScript)
}
