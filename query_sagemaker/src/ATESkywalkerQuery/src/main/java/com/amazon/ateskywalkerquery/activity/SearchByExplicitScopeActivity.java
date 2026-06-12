package com.amazon.ateskywalkerquery.activity;

import com.amazon.ateskywalkerquery.ISearchByExplicitScopeActivity;
import com.amazon.ateskywalkerquery.SearchByExplicitScopeInput;
import com.amazon.ateskywalkerquery.SearchResult;
import com.amazon.ateskywalkerquery.pipeline.QueryPipeline;
import com.amazon.coral.annotation.Service;
import com.amazon.coral.service.Activity;
import com.amazon.coral.service.LogRequests;
import com.google.inject.Inject;

/** Runs the FAQ-only query pipeline for an explicit-scope request. */
@Service("ATESkywalkerQuery")
public class SearchByExplicitScopeActivity extends Activity implements ISearchByExplicitScopeActivity {

    private final QueryPipeline pipeline;

    /**
     * Creates the activity.
     *
     * @param pipeline the query pipeline
     */
    @Inject
    public SearchByExplicitScopeActivity(QueryPipeline pipeline) {
        this.pipeline = pipeline;
    }

    @Override
    @LogRequests
    public SearchResult searchByExplicitScope(SearchByExplicitScopeInput input) {
        return pipeline.execute(input);
    }
}
