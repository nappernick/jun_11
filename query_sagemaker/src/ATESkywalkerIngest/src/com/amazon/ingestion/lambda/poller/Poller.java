package com.amazon.ingestion.lambda.poller;

import com.amazon.ingestion.contract.CandidateIndexHandle;
import com.amazon.ingestion.contract.WorkItemResponse;
import com.amazon.ingestion.corex.CoreXCredentialsProvider;
import com.amazon.ingestion.corex.CoreXGraphQLClient;
import com.amazon.ingestion.corex.CoreXSecretReader;
import com.amazon.ingestion.dispatch.WorkItemDispatcher;
import com.amazon.ingestion.enumeration.CoreXEnumerator;
import com.amazon.ingestion.enumeration.CoreXSearchEnumerator;
import com.amazon.ingestion.heartbeat.PollHeartbeat;
import com.amazon.ingestion.heartbeat.SsmPollHeartbeat;
import com.amazon.ingestion.indexing.IndexManager;
import com.amazon.ingestion.pipeline.RebuildCoordinator;
import com.amazon.ingestion.snapshot.SnapshotMarkerStore;
import com.amazon.ingestion.snapshot.SsmSnapshotMarkerStore;
import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.ssm.SsmClient;
import software.amazon.awssdk.services.sts.StsClient;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * Scheduled entry point for the daily FAQ evidence rebuild.
 *
 * Thin adapter around RebuildCoordinator. All rebuild logic lives in the coordinator;
 * this class only wires collaborators and serializes the outcome.
 *
 * Triggered by EventBridge (daily cron). Input is ignored at launch; future manual-trigger
 * support may read flags from the event payload.
 */
public class Poller implements RequestHandler<Map<String, Object>, Map<String, Object>> {

    private static final Logger LOGGER = LogManager.getLogger(Poller.class);

    private static final String ENV_LAST_POLL = "SSM_LAST_POLL_TIMESTAMP";
    private static final String ENV_LAST_CONTENT_UPDATE = "SSM_LAST_CONTENT_UPDATE_TIMESTAMP";
    private static final String ENV_COREX_ROLE_ARN = "COREX_ROLE_ARN";
    private static final String ENV_COREX_SESSION = "COREX_ROLE_SESSION_PREFIX";
    private static final String ENV_COREX_SECRET = "COREX_SECRET_NAME";
    private static final String ENV_COREX_HOST = "COREX_HOST";
    private static final String ENV_COREX_FAQ_TOPIC = "COREX_FAQ_TOPIC_ID";

    private final RebuildCoordinator coordinator;

    /** Default constructor used by the Lambda runtime. Wires real SSM and COREx, stubs for the rest. */
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

        SnapshotMarkerStore markerStore = new SsmSnapshotMarkerStore(ssm, env(ENV_LAST_CONTENT_UPDATE));
        PollHeartbeat heartbeat = new SsmPollHeartbeat(ssm, env(ENV_LAST_POLL));

        CoreXSecretReader secretReader = new CoreXSecretReader(sm, env(ENV_COREX_SECRET));
        CoreXCredentialsProvider credentials = new CoreXCredentialsProvider(
                sts,
                env(ENV_COREX_ROLE_ARN),
                env(ENV_COREX_SESSION),
                secretReader);
        CoreXGraphQLClient graphql = new CoreXGraphQLClient(credentials, secretReader, env(ENV_COREX_HOST));
        CoreXEnumerator enumerator = new CoreXSearchEnumerator(graphql, env(ENV_COREX_FAQ_TOPIC));

        return new RebuildCoordinator(
                markerStore,
                heartbeat,
                enumerator,
                new StubIndexManager(),
                new StubWorkItemDispatcher());
    }

    private static String env(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException("Required environment variable missing: " + name);
        }
        return value;
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
                "Rebuild run complete runId={} kind={} reason={}",
                outcome.runId(),
                outcome.kind(),
                outcome.reason());

        Map<String, Object> result = new HashMap<>();
        result.put("runId", outcome.runId());
        result.put("kind", outcome.kind().name());
        result.put("snapshotMarker", outcome.snapshotMarker() == null ? "" : outcome.snapshotMarker());
        result.put("childrenIndexed", outcome.childrenIndexed());
        result.put("reason", outcome.reason() == null ? "" : outcome.reason());
        return result;
    }

    // Remaining stubs. Index lifecycle and dispatch land next. Stubs are deliberately safe:
    // they never swap aliases or delete real indexes.

    private static final class StubIndexManager implements IndexManager {
        @Override
        public CandidateIndexHandle createCandidateIndex(String snapshotMarker) {
            return new CandidateIndexHandle("faq_evidence_v1", 1, snapshotMarker);
        }

        @Override
        public void validate(CandidateIndexHandle candidate) {
            LOGGER.warn("StubIndexManager.validate no-op for {}", candidate.indexName());
        }

        @Override
        public void swapAlias(CandidateIndexHandle candidate) {
            LOGGER.warn("StubIndexManager.swapAlias no-op for {}", candidate.indexName());
        }

        @Override
        public void deleteOrphan(CandidateIndexHandle candidate) {
            LOGGER.warn("StubIndexManager.deleteOrphan no-op for {}", candidate.indexName());
        }

        @Override
        public void garbageCollectOldVersions() {
            LOGGER.warn("StubIndexManager.garbageCollectOldVersions no-op");
        }
    }

    private static final class StubWorkItemDispatcher implements WorkItemDispatcher {
        @Override
        public List<WorkItemResponse> dispatch(
                String runId, String candidateIndex, List<String> nodeIds, int groupSize) {
            LOGGER.warn(
                    "StubWorkItemDispatcher.dispatch returning empty responses for runId={} nodes={}",
                    runId,
                    nodeIds.size());
            return List.of();
        }
    }
}
