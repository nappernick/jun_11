package com.amazon.ateskywalkerquery.module;

import com.amazon.cloudauth.CloudAuthRegion;
import com.amazon.cloudauth.client.CloudAuthCredentials;
import com.amazon.coral.authorization.AuthorizationHandler;
import com.amazon.coral.authorization.Authorizer;
import com.amazon.coral.bobcat.Bobcat3EndpointConfig;
import com.amazon.coral.bobcat.Bobcat3EndpointConfig.HealthCheck;
import com.amazon.coral.bobcat.BobcatServer;
import com.amazon.coral.bobcat.SelfSignedKeystoreConfig;
import com.amazon.coral.cloudauth.CloudAuthAuthorizer;
import com.amazon.coral.guice.GuiceActivityHandler;
import com.amazon.coral.guice.health.GuiceActivityHealthCheck;
import com.amazon.coral.metrics.MetricsFactory;
import com.amazon.coral.metrics.emf.EmfConfiguration;
import com.amazon.coral.metrics.emf.EmfDimension;
import com.amazon.coral.metrics.emf.EmfReporterFactory;
import com.amazon.coral.metrics.emf.EmfWriter;
import com.amazon.coral.metrics.helper.SensingMetricsHelper;
import com.amazon.coral.metrics.reporter.ReporterFactory;
import com.amazon.coral.service.BasicShallowHealthCheck;
import com.amazon.coral.service.DefaultHealthCheckStrategy;
import com.amazon.coral.service.HttpHandler;
import com.amazon.coral.service.HttpRpcHandler;
import com.amazon.coral.service.HttpSmithyRpcV2CborHandler;
import com.amazon.coral.service.Log4jAwareRequestIdHandler;
import com.amazon.coral.service.Orchestrator;
import com.amazon.coral.service.PingHandler;
import com.amazon.coral.service.RejectUnclaimedJobHandler;
import com.amazon.coral.service.ServiceHandler;
import com.amazon.coral.service.helper.ChainHelper;
import com.amazon.coral.service.helper.OrchestratorHelper;
import com.amazon.coral.service.http.ContentHandler;
import com.amazon.coral.service.http.CrossOriginHandler;
import com.amazon.coral.validate.ValidationHandler;
import com.google.inject.AbstractModule;
import com.google.inject.Injector;
import com.google.inject.Provides;
import com.google.inject.Singleton;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;

import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.TimeUnit;

import static com.amazon.coral.bobcat.Bobcat3EndpointConfig.uri;

public class CoralModule extends AbstractModule {
    // The default idle timeout on the ALB is 60 seconds, so the server's idle timeout is
    // increased from 20 to 65 seconds to follow the recommendation that the application's
    // idle timeout be configured to be larger than the load balancer's idle timeout.
    // https://docs.aws.amazon.com/elasticloadbalancing/latest/application/application-load-balancers.html#connection-idle-timeout
    private static final Duration IDLE_TIMEOUT = Duration.ofSeconds(65);
    private static final int NUM_THREADS = 32;
    private static final int MAX_REQUEST_SIZE_IN_BYTES = 1024 * 1024 * 1; // 1 mb

    private final String root;
    private final String realm;
    private final String domain;

    public CoralModule(String root, String domain, String realm) {
        this.root = Objects.requireNonNull(root);
        this.realm = Objects.requireNonNull(realm);
        this.domain = Objects.requireNonNull(domain);
    }

    @Provides
    @Singleton
    EmfWriter getEmfWriter() {
        // To significantly reduce CloudWatch Logs usage, at the expense of losing visibility into per-request metrics,
        // you can switch to using EmfWriter.log("emf.metrics").aggregate(every(1, MINUTES))
        // This has no impact to the accuracy of your CloudWatch metrics, only the granularity of data in the EMF logs
        return EmfWriter.log("emf.metrics");
    }

    @Provides
    @Singleton
    MetricsFactory getMetricsFactory(EmfWriter writer) {
        EmfConfiguration configuration = EmfConfiguration.builder()
            .setNamespace(System.getenv("NAMESPACE"))
            .addDimension(EmfDimension.METHOD_NAME)
            .setWriter(writer)
            .build();
        SensingMetricsHelper metricsFactory = new SensingMetricsHelper();
        List<ReporterFactory> reporters = new ArrayList<>();
        reporters.add(EmfReporterFactory.of(configuration));
        if (Boolean.parseBoolean(System.getenv("IS_ONEPOD"))) {
            EmfConfiguration onePodConfiguration = EmfConfiguration.builder()
                .from(configuration)
                .setNamespace(System.getenv("NAMESPACE") + "-OnePod")
                .build();
            reporters.add(EmfReporterFactory.of(onePodConfiguration));
        }
        metricsFactory.setReporters(reporters);
        return metricsFactory;
    }

    @Provides
    @Singleton
    BobcatServer getBobcatServer(Orchestrator coral, MetricsFactory metricsFactory, Injector injector) {
        Bobcat3EndpointConfig endpointConfig = new Bobcat3EndpointConfig();

        BasicShallowHealthCheck shallowStrategy = new BasicShallowHealthCheck();
        GuiceActivityHealthCheck deepStrategy = new GuiceActivityHealthCheck(injector);

        HealthCheck defaultHealthCheck = new HealthCheck(HealthCheck.Protocol.HTTP, 8081, false)
            .healthCheckStrategy(new DefaultHealthCheckStrategy(shallowStrategy, deepStrategy)::isHealthy);

        endpointConfig.setMetricsFactory(metricsFactory);
        endpointConfig.setOrchestrator(coral);
        endpointConfig.setNumThreads(NUM_THREADS);
        endpointConfig.setEndpoints(List.of(uri("http://0.0.0.0:8080"), uri("https://0.0.0.0:8443")));
        endpointConfig.setKeystoreConfig(new SelfSignedKeystoreConfig());
        endpointConfig.setEndpointHealthChecks(List.of(defaultHealthCheck));
        endpointConfig.setOverrideRequestId(true);
        endpointConfig.setIdleTimeout(IDLE_TIMEOUT);
        endpointConfig.setMaxRequestSize(MAX_REQUEST_SIZE_IN_BYTES);
        endpointConfig.legacySslVipCompatibilityMode(false);
        return new BobcatServer(endpointConfig);
    }

    @Provides
    @Singleton
    Orchestrator getOrchestrator(MetricsFactory metricsFactory, Injector injector) throws Exception {
        ChainHelper chainHelper = new ChainHelper();
        chainHelper.addHandler(new Log4jAwareRequestIdHandler());
        chainHelper.addHandler(new HttpHandler());
        chainHelper.addHandler(new CrossOriginHandler());
        // This PingHandler is currently required to avoid spurious InternalFailure logs
        // during shutdown. Follow a fix here: https://issues.amazon.com/issues/CORAL-2833
        chainHelper.addHandler(new PingHandler());

        if (Boolean.getBoolean("coral.explorer.enable")) {
            // The ContentHandler serves the static data for Coral Explorer.
            // Coral Explorer can be a security risk if exposed.
            ContentHandler contentHandler = new ContentHandler.Builder()
                .withDirectories(root + "/static-content")
                .build();
            chainHelper.addHandler(contentHandler);
        }

        chainHelper.addHandler(new ServiceHandler("ATESkywalkerQuery"));

        chainHelper.addHandler(new HttpRpcHandler());
        chainHelper.addHandler(new HttpSmithyRpcV2CborHandler());
        chainHelper.addHandler(new RejectUnclaimedJobHandler());

        if (!Boolean.getBoolean("cloudauth.disable")) {
            CloudAuthRegion cloudAuthRegion = CloudAuthRegion.from(realm);
            AwsCredentialsProvider credentialsProvider =
                DefaultCredentialsProvider.builder().build();
            CloudAuthCredentials credentials =
                CloudAuthCredentials.RegionalAwsCredentials.create(credentialsProvider, cloudAuthRegion);
            Authorizer cloudAuthAuthorizer =
                CloudAuthAuthorizer.builder(credentials, metricsFactory).build();
            List<Authorizer> authorizers = Collections.singletonList(cloudAuthAuthorizer);
            chainHelper.addHandler(new AuthorizationHandler(authorizers));
        }

        chainHelper.addHandler(new ValidationHandler(true));
        chainHelper.addHandler(GuiceActivityHandler.create(injector));
        return new OrchestratorHelper(chainHelper, TimeUnit.SECONDS.toMillis(30));
    }
}
