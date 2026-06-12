package com.amazon.ingestion.indexing;

public interface FragmentIndexWriter {
    void write(String indexName, FragmentDocument document);
}
