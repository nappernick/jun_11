package com.amazon.ateskywalkerquery.embedding;

import java.util.ArrayList;
import java.util.List;

/** In-memory {@link EmbeddingClient} for tests: returns a canned vector, records the input. */
public class FakeEmbeddingClient implements EmbeddingClient {
    private List<Float> vector = new ArrayList<>();
    private String lastText;

    public void setVector(List<Float> vector) {
        this.vector = new ArrayList<>(vector);
    }

    public String lastText() {
        return lastText;
    }

    @Override
    public List<Float> embed(String text) {
        this.lastText = text;
        return new ArrayList<>(vector);
    }
}
