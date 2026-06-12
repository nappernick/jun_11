package com.amazon.ingestion.lambda.poller;

import com.amazon.ingestion.corex.CoreXCredentialsProvider;
import com.amazon.ingestion.corex.CoreXGraphQLClient;
import com.amazon.ingestion.corex.CoreXSecretReader;
import com.amazon.ingestion.dispatch.LambdaWorkItemDispatcher;
import com.amazon.ingestion.enumeration.CoreXEnumerator;
import com.amazon.ingestion.enumeration.CoreXSearchEnumerator;
import com.amazon.ingestion.indexing.OpenSearchIndexManager;
import com.amazon.ingestion.pipeline.RebuildCoordinator;
import com.amazon.ingestion.snapshot.SnapshotMarkerStore;
import com.amazon.ingestion.snapshot.SsmSnapshotMarkerStore;
import com.amazon.ingestion.snapshot.LiveIndexStore;
import com.amazon.ingestion.snapshot.SsmLiveIndexStore;
import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.lambda.LambdaClient;
import software.amazon.awssdk.services.ssm.SsmClient;
import software.amazon.awssdk.services.sts.StsClient;

import java.util.HashMap;
import java.util.Map;

/**
 * Scheduled entry point for the daily FAQ evidence rebuild.
 *
 * Thin adapter around RebuildCoordinator. All rebuild logic lives in the coordinator;
 * this class only wires collaborators and serializes the outcome.
 *
 * Triggered by EventBridge (daily cron) or invoked manually. Input is ignored at launch.
 */
public class Poller implements RequestHandler<Map<String, Object>, Map<String, Object>> {

    private static final Logger LOGGER = LogManager.getLogger(Poller.class);

    private static final String ENV_SNAPSHOT_MARKER = "SSM_SNAPSHOT_MARKER";
    private static final String ENV_LIVE_INDEX = "SSM_LIVE_INDEX";
    private static final String ENV_COREX_ROLE_ARN = "COREX_ROLE_ARN";
    private static final String ENV_COREX_SESSION = "COREX_ROLE_SESSION_PREFIX";
    private static final String ENV_COREX_SECRET = "COREX_SECRET_NAME";
    private static final String ENV_COREX_HOST = "COREX_HOST";
    private static final String ENV_COREX_DOMAIN_OWNER_ID = "COREX_DOMAIN_OWNER_ID";
    private static final String ENV_PROCESSOR_FUNCTION_NAME = "PROCESSOR_FUNCTION_NAME";
    private static final String ENV_OPENSEARCH_ENDPOINT = "OPENSEARCH_ENDPOINT";
    private static final String ENV_AWS_REGION = "AWS_REGION";
    private static final String DEFAULT_REGION = "us-west-2";
    private static final String INDEX_BASE_NAME = "faq_evidence";

    private final RebuildCoordinator coordinator;

    /** Default constructor used by the Lambda runtime. Wires real SSM, COREx, OpenSearch, and Lambda dispatch. */
    public Poller() {
        this(buildDefaultCoordinator());
    }

    /** Test/injection constructor. */
    Poller(RebuildCoordinator coordinator) {
        this.coordinator = coordinator;
    }

    private static RebuildCoordinator buildDefaultCoordinator() {
        SsmClient ssm = SsmClient.create();
        SecretsManagerClient sm = SecretsManagerClient.create();
        StsClient sts = StsClient.create();
        LambdaClient lambda = LambdaClient.create();

        SnapshotMarkerStore markerStore = new SsmSnapshotMarkerStore(ssm, env(ENV_SNAPSHOT_MARKER));
        LiveIndexStore liveIndexStore = new SsmLiveIndexStore(ssm, env(ENV_LIVE_INDEX));

        CoreXSecretReader secretReader = new CoreXSecretReader(sm, env(ENV_COREX_SECRET));
        CoreXCredentialsProvider credentials = new CoreXCredentialsProvider(
                sts,
                env(ENV_COREX_ROLE_ARN),
                env(ENV_COREX_SESSION),
                secretReader);
        CoreXGraphQLClient graphql = new CoreXGraphQLClient(credentials, secretReader, env(ENV_COREX_HOST));
        CoreXEnumerator enumerator = new CoreXSearchEnumerator(graphql, env(ENV_COREX_DOMAIN_OWNER_ID));

        return new RebuildCoordinator(
                markerStore,
                enumerator,
                new OpenSearchIndexManager(
                        env(ENV_OPENSEARCH_ENDPOINT),
                        envOrDefault(ENV_AWS_REGION, DEFAULT_REGION),
                        INDEX_BASE_NAME,
                        liveIndexStore),
                new LambdaWorkItemDispatcher(lambda, env(ENV_PROCESSOR_FUNCTION_NAME)));
    }

    private static String env(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("Required environment variable missing: " + name);
        }
        return value;
    }

    private static String envOrDefault(String name, String defaultValue) {
        String value = System.getenv(name);
        return value == null || value.isBlank() ? defaultValue : value;
    }

    @Override
    public Map<String, Object> handleRequest(Map<String, Object> event, Context context) {
        String awsRequestId = "<none>";
        if (context != null) {
            awsRequestId = context.getAwsRequestId();
        }
        LOGGER.info("Poller invoked requestId={}", awsRequestId);

        RebuildCoordinator.RunOutcome outcome = coordinator.run();
        LOGGER.info(
                "Rebuild run complete runId={} kind={} marker={} fragmentsIndexed={} nodesSkipped={}",
                outcome.runId(),
                outcome.kind(),
                outcome.snapshotMarker(),
                outcome.fragmentsIndexed(),
                outcome.nodesSkipped());

        Map<String, Object> result = new HashMap<>();
        result.put("runId", outcome.runId());
        result.put("kind", outcome.kind().name());
        result.put("snapshotMarker", outcome.snapshotMarker() == null ? "" : outcome.snapshotMarker());
        result.put("fragmentsIndexed", outcome.fragmentsIndexed());
        result.put("nodesSkipped", outcome.nodesSkipped());
        return result;
    }
}
