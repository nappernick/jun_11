package com.amazon.ingestion.corex;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.util.ArrayList;
import java.util.List;

/**
 * Extracts embeddable plain text from a COREx content node body.
 *
 * <h2>Real content shape (proven against live COREx, node 38b86658-…, 2026-06-03)</h2>
 * COREx rich-text bodies are <b>PlateJS</b> (a Slate-derived rich-text JSON), reached via the
 * {@code getContentNodes} (plural) endpoint. The node's {@code content} field is a JSON
 * <em>string</em> shaped:
 *
 * <pre>
 * { "system_rte_v2": { "type": "RTE_V2", "content": "&lt;JSON-string PlateJS array&gt;" } }
 * </pre>
 *
 * The inner {@code content} is itself a JSON string holding an array of PlateJS block nodes.
 * Each block is {@code {"type": "...", "children": [ ... ]}}; text lives in leaf nodes
 * {@code {"text": "..."}} (optionally carrying inline marks such as {@code "bold": true}).
 * Real block types observed: {@code h1, h2, h3, p, ul, li, a, toc, dam-file}. Links ({@code a})
 * nest their visible text in {@code children}; the URL is carried on the {@code url} attribute.
 *
 * <h2>Extraction strategy</h2>
 * Walk the PlateJS tree depth-first and collect every {@code text} leaf in document order,
 * joining block-level runs with single spaces. Inline marks (bold/italic/etc.) do not change
 * the extracted text — embedding and BM25 want clean prose, not formatting. Recursion over
 * {@code children} means new/unknown block types degrade gracefully (their text is still
 * collected) rather than being dropped.
 *
 * <p><b>In-text links are preserved.</b> For a link ({@code a}) node we keep both the visible
 * link text and its {@code url}, emitted as {@code "link text (https://url)"}. FAQ answers
 * frequently <em>are</em> a pointer to a policy page or tool, so the URL is load-bearing
 * evidence: keeping it inline means it is embedded into the vector and matchable by BM25
 * (e.g. a query naming a policy URL or domain), not just discarded as formatting.
 *
 * <p>Two real envelope shapes are handled:
 * <ul>
 *   <li><b>RTE_V2 / PlateJS</b> — the real COREx shape above: an object whose RTE_V2 field's
 *       {@code content} is a JSON-string PlateJS array. This is the production path.</li>
 *   <li><b>Plain string</b> — if the body ever arrives as a bare string, it is returned as
 *       normalized text unchanged (no markup assumptions).</li>
 * </ul>
 */
public final class CoreXTextExtractor {

    private static final ObjectMapper JSON = new ObjectMapper();

    public String extract(CoreXContentNode node) {
        JsonNode content = node.content();
        List<String> parts = new ArrayList<>();

        if (content != null && content.isTextual()) {
            // Bare string body: no markup assumptions, return as-is (normalized).
            return normalize(content.asText());
        }
        collectFromContentObject(content, parts);
        return normalize(String.join(" ", parts));
    }

    /**
     * Walk the top-level content object's fields. The real shape nests the body under an
     * RTE_V2 field ({@code system_rte_v2}); other fields are walked too so nothing is missed.
     */
    private static void collectFromContentObject(JsonNode content, List<String> parts) {
        if (content == null || !content.isObject()) {
            return;
        }
        content.fields().forEachRemaining(entry -> collectField(entry.getValue(), parts));
    }

    private static void collectField(JsonNode field, List<String> parts) {
        if (field == null || field.isNull()) {
            return;
        }
        String type = field.path("type").asText("");
        if ("RTE_V2".equals(type)) {
            // The RTE_V2 content is a JSON string holding the PlateJS block array.
            collectPlateJs(field.path("content"), parts);
            return;
        }
        // Any other field: walk it for text leaves defensively.
        collectTextLeaves(field, parts);
    }

    /**
     * Parse the RTE_V2 {@code content} (a JSON-string PlateJS array) and collect its text.
     * If it is not parseable JSON, fall back to treating it as raw text.
     *
     * @param rteContent the RTE_V2 content node (a JSON string in the real shape).
     * @param parts      accumulator for extracted text runs.
     */
    private static void collectPlateJs(JsonNode rteContent, List<String> parts) {
        if (rteContent == null || rteContent.isNull()) {
            return;
        }
        JsonNode plate;
        if (rteContent.isTextual()) {
            try {
                plate = JSON.readTree(rteContent.asText());
            } catch (Exception e) {
                // Not JSON — treat the string itself as text.
                String raw = rteContent.asText();
                if (!raw.isBlank()) {
                    parts.add(raw);
                }
                return;
            }
        } else {
            plate = rteContent;
        }
        collectBlocks(plate, parts);
    }

    /**
     * Collect text from a PlateJS structure, joining block-level runs into separate parts so
     * adjacent blocks (e.g. a heading followed by a paragraph) do not run together.
     *
     * @param plate a PlateJS array of blocks, or a single block node.
     * @param parts accumulator for extracted text runs (one per block).
     */
    private static void collectBlocks(JsonNode plate, List<String> parts) {
        if (plate == null || plate.isNull()) {
            return;
        }
        if (plate.isArray()) {
            for (JsonNode block : plate) {
                String blockText = textOf(block);
                if (!blockText.isBlank()) {
                    parts.add(blockText);
                }
            }
        } else {
            String blockText = textOf(plate);
            if (!blockText.isBlank()) {
                parts.add(blockText);
            }
        }
    }

    /**
     * Depth-first concatenation of all {@code text} leaves under a single PlateJS block, in
     * document order, joined by single spaces between leaves. Inline marks are ignored (we keep
     * the text, drop the formatting). Unknown block/leaf types are traversed via children, so
     * future PlateJS additions still contribute their text.
     *
     * @param block a PlateJS node.
     * @return the block's plain text.
     */
    private static String textOf(JsonNode block) {
        List<String> leaves = new ArrayList<>();
        collectTextLeaves(block, leaves);
        return String.join(" ", leaves);
    }

    private static void collectTextLeaves(JsonNode node, List<String> leaves) {
        if (node == null || node.isNull()) {
            return;
        }
        if (node.isObject()) {
            JsonNode text = node.get("text");
            if (text != null && text.isTextual()) {
                String value = text.asText();
                if (!value.isBlank()) {
                    leaves.add(value.trim());
                }
            }
            // Link node: keep the visible child text AND the URL inline, so the link target is
            // embedded and BM25-searchable (FAQ answers often point at a policy page or tool).
            if ("a".equals(node.path("type").asText("")) && node.path("url").isTextual()) {
                String url = node.path("url").asText("").trim();
                JsonNode children = node.get("children");
                if (children != null) {
                    collectTextLeaves(children, leaves);
                }
                if (!url.isBlank()) {
                    leaves.add("(" + url + ")");
                }
                return;
            }
            JsonNode children = node.get("children");
            if (children != null) {
                collectTextLeaves(children, leaves);
            }
        } else if (node.isArray()) {
            for (JsonNode child : node) {
                collectTextLeaves(child, leaves);
            }
        }
    }

    private static String normalize(String raw) {
        // Collapse all whitespace (including the non-breaking spaces COREx emits, \u00a0) to
        // single spaces and trim.
        if (raw == null) {
            return "";
        }
        return raw.replace('\u00a0', ' ').replaceAll("\\s+", " ").trim();
    }
}
