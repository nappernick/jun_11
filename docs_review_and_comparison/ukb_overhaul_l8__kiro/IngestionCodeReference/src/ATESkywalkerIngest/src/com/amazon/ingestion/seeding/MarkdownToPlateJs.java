package com.amazon.ingestion.seeding;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Converts a markdown string into the COREx <b>PlateJS</b> block array (a JSON string), shaped
 * exactly like the real {@code system_rte_v2.content} payload captured live from COREx
 * (node 38b86658-..., 2026-06-03).
 *
 * <h2>Why this exists (seeding only)</h2>
 * Production receives PlateJS directly from COREx, so the pipeline never converts markdown.
 * But the local prod export ({@code source_with_content.jsonl}) carries an HTML-scraped
 * <em>markdown</em> body, not PlateJS. To seed the alpha collection with data that is
 * byte-for-byte what the real pipeline will eventually produce, we first lift that markdown
 * into the genuine PlateJS shape, then run it through the unchanged production chain
 * (extractor -> scope -> embed -> write). This converter is a <b>seeding tool</b>, not part of
 * the ingestion pipeline.
 *
 * <h2>Coverage</h2>
 * Matches the block/inline vocabulary observed in the real corpus: ATX headings
 * ({@code #}, {@code ##}, {@code ###} become {@code h1}/{@code h2}/{@code h3}), unordered
 * lists (dash/asterisk/plus bullets become {@code ul}/{@code li}), paragraphs ({@code p}),
 * inline links ({@code [text](url)} become {@code a} with {@code url}), and bold runs become
 * a {@code text} leaf with {@code "bold":true}. Ordered lists and tables are rare in the
 * corpus and degrade to paragraphs (their text is preserved). Every block carries a short
 * {@code id} like real PlateJS.
 */
public final class MarkdownToPlateJs {

    private static final ObjectMapper JSON = new ObjectMapper();

    private static final Pattern HEADING = Pattern.compile("^(#{1,6})\\s+(.*)$");
    private static final Pattern LIST_ITEM = Pattern.compile("^\\s*[-*+]\\s+(.*)$");
    private static final Pattern LINK = Pattern.compile("\\[([^\\]]+)\\]\\(([^)]+)\\)");
    private static final Pattern BOLD = Pattern.compile("\\*\\*([^*]+)\\*\\*");

    private MarkdownToPlateJs() {
    }

    /**
     * Convert markdown to a PlateJS block array serialized as a JSON string (the exact form
     * COREx stores under {@code system_rte_v2.content}).
     *
     * @param markdown the markdown body.
     * @return a JSON string holding the PlateJS block array.
     */
    public static String convert(String markdown) {
        ArrayNode blocks = JSON.createArrayNode();
        if (markdown == null || markdown.isBlank()) {
            return blocks.toString();
        }

        String[] lines = markdown.replace("\r\n", "\n").split("\n");
        ArrayNode currentList = null;

        for (String rawLine : lines) {
            String line = rawLine.strip();
            if (line.isEmpty()) {
                currentList = null;
                continue;
            }

            Matcher heading = HEADING.matcher(line);
            if (heading.matches()) {
                currentList = null;
                int level = Math.min(heading.group(1).length(), 3);
                ObjectNode block = newBlock("h" + level);
                fillInline(block, heading.group(2));
                blocks.add(block);
                continue;
            }

            Matcher li = LIST_ITEM.matcher(rawLine);
            if (li.matches()) {
                if (currentList == null) {
                    ObjectNode ul = newBlock("ul");
                    currentList = ul.putArray("children");
                    blocks.add(ul);
                }
                ObjectNode liBlock = JSON.createObjectNode();
                liBlock.put("type", "li");
                liBlock.put("id", shortId());
                fillInline(liBlock, li.group(1));
                currentList.add(liBlock);
                continue;
            }

            // Default: a paragraph.
            currentList = null;
            ObjectNode p = newBlock("p");
            fillInline(p, line);
            blocks.add(p);
        }

        return blocks.toString();
    }

    /**
     * Wrap the converted PlateJS array in the full COREx content envelope:
     * {@code {"system_rte_v2":{"type":"RTE_V2","content":"<plate json string>"}}}, returned as
     * a JSON string (the exact shape of the COREx node {@code content} field).
     *
     * @param markdown the markdown body.
     * @return the RTE_V2 content envelope as a JSON string.
     */
    public static String toContentEnvelope(String markdown) {
        ObjectNode rte = JSON.createObjectNode();
        rte.put("type", "RTE_V2");
        rte.put("content", convert(markdown));
        ObjectNode envelope = JSON.createObjectNode();
        envelope.set("system_rte_v2", rte);
        return envelope.toString();
    }

    private static ObjectNode newBlock(String type) {
        ObjectNode block = JSON.createObjectNode();
        block.put("type", type);
        block.put("id", shortId());
        return block;
    }

    /**
     * Parse inline markdown (links + bold) into PlateJS leaf/inline children on the block.
     * Text outside links is split on bold runs; links become {@code a} nodes carrying the URL
     * and a child text leaf.
     */
    private static void fillInline(ObjectNode block, String text) {
        ArrayNode children = block.putArray("children");
        int pos = 0;
        Matcher link = LINK.matcher(text);
        while (link.find()) {
            if (link.start() > pos) {
                addTextRuns(children, text.substring(pos, link.start()));
            }
            ObjectNode a = JSON.createObjectNode();
            a.put("type", "a");
            a.put("url", link.group(2).trim());
            a.put("target", "_blank");
            ArrayNode aChildren = a.putArray("children");
            addTextRuns(aChildren, link.group(1));
            children.add(a);
            pos = link.end();
        }
        if (pos < text.length()) {
            addTextRuns(children, text.substring(pos));
        }
        if (children.isEmpty()) {
            children.add(textLeaf("", false));
        }
    }

    /**
     * Split a text run on bold markers, emitting {@code text} leaves (bold leaves carry
     * {@code "bold":true}), matching real PlateJS leaves.
     */
    private static void addTextRuns(ArrayNode children, String segment) {
        if (segment.isEmpty()) {
            return;
        }
        int pos = 0;
        Matcher bold = BOLD.matcher(segment);
        while (bold.find()) {
            if (bold.start() > pos) {
                children.add(textLeaf(segment.substring(pos, bold.start()), false));
            }
            children.add(textLeaf(bold.group(1), true));
            pos = bold.end();
        }
        if (pos < segment.length()) {
            children.add(textLeaf(segment.substring(pos), false));
        }
    }

    private static ObjectNode textLeaf(String text, boolean bold) {
        ObjectNode leaf = JSON.createObjectNode();
        leaf.put("text", text);
        if (bold) {
            leaf.put("bold", true);
        }
        return leaf;
    }

    private static String shortId() {
        // PlateJS-style short alphanumeric id; not load-bearing for extraction.
        return Long.toString(Math.abs(System.nanoTime() % 1_000_000_000L), 36) + (idCounter++);
    }

    private static long idCounter = 0;
}
