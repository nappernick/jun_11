package com.amazon.ateskywalkerquery.activity;

import com.amazon.ateskywalkerquery.CorpusResponse;
import com.amazon.ateskywalkerquery.IGetCorpus2Activity;
import com.amazon.coral.annotation.Service;
import com.amazon.coral.service.Activity;
import com.amazon.coral.service.LogRequests;

import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

@Service("ATESkywalkerQuery")
public class GetCorpus2Activity extends Activity implements IGetCorpus2Activity {

    private static final String CORPUS;

    static {
        try (InputStream is = GetCorpus2Activity.class.getResourceAsStream("/corpus2.json")) {
            CORPUS = new String(is.readAllBytes(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new RuntimeException("Failed to load corpus2.json", e);
        }
    }

    @Override
    @LogRequests
    public CorpusResponse getCorpus2() {
        CorpusResponse response = new CorpusResponse();
        response.setContent(CORPUS);
        return response;
    }
}
