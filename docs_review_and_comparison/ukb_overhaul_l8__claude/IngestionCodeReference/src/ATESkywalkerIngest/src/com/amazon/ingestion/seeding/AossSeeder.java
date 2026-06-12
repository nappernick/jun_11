package com.amazon.ingestion.seeding;

import com.amazon.ingestion.corex.ContentNodeFetcher;
import com.amazon.ingestion.corex.CoreXContentNode;
import com.amazon.ingestion.corex.CoreXTextExtractor;
import com.amazon.ingestion.embedding.BedrockEmbeddingClient;
import com.amazon.ingestion.indexing.IndexManager;
import com.amazon.ingestion.indexing.OpenSearchFragmentWriter;
import com.amazon.ingestion.indexing.OpenSearchIndexManager;
import com.amazon.ingestion.processor.FragmentProcessor;
import com.amazon.ingestion.schema.CorpusSchema;
import com.amazon.ingestion.snapshot.LiveIndexStore;
import com.amazon.ingestion.snapshot.SsmLiveIndexStore;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import software.amazon.awssdk.services.ssm.SsmClient;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * One-off seeder that writes the durable PlateJS corpus into the alpha AOSS collection by
 * running each record through the <b>exact production chain</b>
 * (extractor -> scope -> metadata assembler -> embed -> write). Run as a developer admin
 * identity, NOT the Lambda role: admin holds the Bedrock inference-profile permission and AOSS
 * access that the (stale) deployed Lambda role lacks, so this seeds correct data without a
 * code deploy.
 *
 * <p>The records come from {@code skywalker-faq-platejs.jsonl} (see
 * {@link PlateJsCorpusGenerator}); their {@code content} is the genuine COREx PlateJS RTE_V2
 * envelope, so the documents written here are byte-for-byte what the real pipeline will
 * eventually produce from live COREx prod.
 *
 * <p>Usage: {@code AossSeeder <platejs.jsonl> <endpoint> <index> <region> <modelId> <corpusVersion>}.
 * Performs real Bedrock + AOSS calls using the ambient AWS credentials.
 */
public final class AossSeeder {

    private static final ObjectMapper JSON = new ObjectMapper();

    private AossSeeder() {
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 6) {
            System.err.println("usage: AossSeeder <platejs.jsonl> <endpoint> <baseName> <region> <model> <version>");
            System.exit(2);
        }
        Path corpus = Path.of(args[0]);
        String endpoint = args[1];
        String baseName = args[2];
        String region = args[3];
        String modelId = args[4];
        String corpusVersion = args[5];
        // Optional 7th arg: SSM live-index pointer name (drives the zero-downtime flip). When
        // omitted, the seeder still builds + promotes but tracks the pointer in-memory only.
        String liveIndexParam = args.length > 6 ? args[6] : null;

        List<CoreXContentNode> nodes = load(corpus);
        System.out.println("Loaded " + nodes.size() + " PlateJS records from " + corpus);

        LiveIndexStore liveIndexStore = liveIndexParam != null
                ? new SsmLiveIndexStore(SsmClient.create(), liveIndexParam)
                : new InMemoryLiveIndexStore();
        IndexManager indexManager = new OpenSearchIndexManager(endpoint, region, baseName, liveIndexStore);

        // Zero-downtime (T14): build into the IDLE index; the currently-live one keeps serving.
        String priorLive = indexManager.liveIndexName().orElse("<none>");
        String target = indexManager.beginRebuild();
        System.out.println("Seeding into idle index=" + target + " (prior live=" + priorLive + ")");

        // Real production components, wired to admin-credentialed clients.
        ContentNodeFetcher fetcher = new InMemoryFetcher(nodes);
        FragmentProcessor processor = new FragmentProcessor(
                fetcher,
                new CoreXTextExtractor(),
                new BedrockEmbeddingClient(region, modelId),
                new OpenSearchFragmentWriter(endpoint, region));

        int written = 0;
        int skipped = 0;
        for (CoreXContentNode node : nodes) {
            try {
                long n = processor.process(target, node.nodeId(), corpusVersion);
                if (n > 0) {
                    written++;
                    System.out.println("WROTE  " + node.nodeId());
                } else {
                    skipped++;
                    System.out.println("SKIP   " + node.nodeId() + " (no text or missing scope)");
                }
            } catch (RuntimeException e) {
                skipped++;
                System.out.println("ERROR  " + node.nodeId() + " : " + e.getMessage());
            }
        }
        System.out.println("Seed complete: written=" + written + " skipped=" + skipped + " of " + nodes.size());

        // FLIP GATE + atomic promote (T14): only promote a non-empty build over the live one.
        // A freshly-created AOSS index has a cold first refresh (~64s measured) before written
        // docs become countable, so poll up to ~100s before deciding (avoids a refresh race).
        long built = 0L;
        for (int attempt = 0; attempt < 50; attempt++) {
            built = indexManager.readBackCount(target);
            if (built > 0) {
                break;
            }
            Thread.sleep(2000L);
        }
        if (built > 0) {
            indexManager.promote(target);
            System.out.println("PROMOTED " + target + " to live (" + built + " docs). Prior live=" + priorLive);
        } else {
            System.out.println("NOT PROMOTING " + target + " (read-back=" + built
                    + "); live index unchanged (still " + priorLive + ")");
        }
    }

    /**
     * Build CoreXContentNodes from the PlateJS corpus exactly as the live CoreXContentFetcher
     * would: parse the metadata + content JSON strings, carry the full raw envelope.
     */
    private static List<CoreXContentNode> load(Path corpus) throws Exception {
        List<CoreXContentNode> out = new ArrayList<>();
        for (String line : Files.readAllLines(corpus, StandardCharsets.UTF_8)) {
            if (line.isBlank()) {
                continue;
            }
            JsonNode r = JSON.readTree(line);
            JsonNode metadata = parseJsonString(r.path("metadata"));
            JsonNode content = parseJsonString(r.path("content"));

            // Raw top-level envelope so MetadataAssembler preserves all top-level fields
            // (minus body content, which is embedded separately).
            ObjectNode raw = JSON.createObjectNode();
            raw.put("nodeId", r.path("nodeId").asText());
            raw.put("title", r.path("title").asText(""));
            raw.put("version", r.path("version").asText(""));
            raw.put("status", r.path("status").asText("DRAFT"));
            raw.set("geography", r.path("geography"));
            raw.set("topics", r.path("topics"));
            if (r.has("lineOfBusiness")) {
                raw.set("lineOfBusiness", r.get("lineOfBusiness"));
            }
            if (r.has("lastModifiedDate")) {
                raw.set("lastModifiedDate", r.get("lastModifiedDate"));
            }

            out.add(new CoreXContentNode(
                    r.path("nodeId").asText(),
                    r.path("version").asText(""),
                    r.path("status").asText("DRAFT"),
                    stringList(r.path("geography")),
                    stringList(r.path("topics")),
                    metadata,
                    content,
                    CorpusSchema.DOMAIN_OWNER,
                    "",
                    true,
                    raw));
        }
        return out;
    }

    private static JsonNode parseJsonString(JsonNode node) throws Exception {
        if (node == null || node.isNull() || node.isMissingNode()) {
            return JSON.createObjectNode();
        }
        if (node.isTextual()) {
            String raw = node.asText();
            return raw.isBlank() ? JSON.createObjectNode() : JSON.readTree(raw);
        }
        return node;
    }

    private static List<String> stringList(JsonNode node) {
        List<String> out = new ArrayList<>();
        if (node != null && node.isArray()) {
            for (JsonNode item : node) {
                if (item != null && item.isValueNode() && !item.asText().isBlank()) {
                    out.add(item.asText());
                }
            }
        }
        return out;
    }

    /** In-memory fetcher that serves the preloaded nodes by id. */
    private static final class InMemoryFetcher implements ContentNodeFetcher {
        private final java.util.Map<String, CoreXContentNode> byId = new java.util.LinkedHashMap<>();

        InMemoryFetcher(List<CoreXContentNode> nodes) {
            for (CoreXContentNode n : nodes) {
                byId.put(n.nodeId(), n);
            }
        }

        @Override
        public CoreXContentNode fetch(String nodeId) {
            CoreXContentNode n = byId.get(nodeId);
            if (n == null) {
                throw new IllegalArgumentException("no seeded node " + nodeId);
            }
            return n;
        }
    }

    /** Fallback live-index pointer when no SSM param is supplied (build+promote, in-memory). */
    private static final class InMemoryLiveIndexStore implements LiveIndexStore {
        private String live;

        @Override
        public java.util.Optional<String> read() {
            return java.util.Optional.ofNullable(live);
        }

        @Override
        public void write(String indexName) {
            this.live = indexName;
        }
    }
}
