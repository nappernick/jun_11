package com.amazon.ateskywalkerquery.activity;

import com.amazon.ateskywalkerquery.CorpusResponse;
import com.amazon.ateskywalkerquery.IGetCorpusActivity;
import com.amazon.coral.annotation.Service;
import com.amazon.coral.service.Activity;
import com.amazon.coral.service.LogRequests;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

@Service("ATESkywalkerQuery")
public class GetCorpusActivity extends Activity implements IGetCorpusActivity {

    private static final String CORPUS;

    static {
        try (InputStream is = GetCorpusActivity.class.getResourceAsStream("/corpus.txt")) {
            CORPUS = new String(is.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new RuntimeException("Failed to load corpus.txt", e);
        }
    }

    @Override
    @LogRequests
    public CorpusResponse getCorpus() {
        CorpusResponse response = new CorpusResponse();
        response.setContent(CORPUS);
        return response;
    }
}
