package com.amazon.ingestion.corex;

public interface ContentNodeFetcher {
    CoreXContentNode fetch(String nodeId);
}
