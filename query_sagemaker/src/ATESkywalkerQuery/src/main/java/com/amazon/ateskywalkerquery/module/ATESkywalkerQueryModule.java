package com.amazon.ateskywalkerquery.module;

import com.amazon.ateskywalkerquery.embedding.BedrockEmbeddingClient;
import com.amazon.ateskywalkerquery.embedding.EmbeddingClient;
import com.amazon.ateskywalkerquery.pipeline.QueryPipeline;
import com.amazon.ateskywalkerquery.rerank.BedrockRerankClient;
import com.amazon.ateskywalkerquery.rerank.RerankClient;
import com.amazon.ateskywalkerquery.retrieval.AossHybridRetrievalClient;
import com.amazon.ateskywalkerquery.retrieval.HybridRetrievalClient;
import com.amazon.guice.brazil.AppConfigBinder;
import com.google.inject.AbstractModule;
import com.google.inject.Provides;
import com.google.inject.Singleton;
import software.amazon.awssdk.auth.credentials.AwsCredentialsProvider;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.auth.StsAssumeRoleCredentialsProvider;
import software.amazon.awssdk.services.sts.model.AssumeRoleRequest;

/**
 * Configures application-specific dependencies for injection: the query pipeline and its
 * AWS clients (Bedrock embed, Bedrock rerank, AOSS hybrid retrieval). Values are read from
 * environment with defaults; production maps these to AppConfig/SSM.
 */
public class ATESkywalkerQueryModule extends AbstractModule {
    private static final String DEFAULT_AOSS_ENDPOINT = "https://3z3yxvl1s09ylso0dgh.us-west-2.aoss.amazonaws.com";

    @Override
    protected void configure() {
        AppConfigBinder appConfigBinder = new AppConfigBinder(binder());
        appConfigBinder.bindPrefix("*");
    }

    @Provides
    @Singleton
    EmbeddingClient embeddingClient() {
        return new BedrockEmbeddingClient(
            env("EMBED_MODEL_ID", "cohere.embed-v4:0"), region(), DefaultCredentialsProvider.create());
    }

    @Provides
    @Singleton
    RerankClient rerankClient() {
        return new BedrockRerankClient(
            env("RERANK_MODEL_ARN", "arn:aws:bedrock:" + region() + "::foundation-model/cohere.rerank-v3-5:0"),
            region(),
            DefaultCredentialsProvider.create(),
            Boolean.parseBoolean(env("RERANK_DIAGNOSTICS_ENABLED", "true")),
            Integer.parseInt(env("RERANK_CONTEXT_WINDOW_TOKENS", "4096")),
            Double.parseDouble(env("RERANK_CHARS_PER_TOKEN", "4.0")));
    }

    @Provides
    @Singleton
    HybridRetrievalClient retrievalClient() {
        return new AossHybridRetrievalClient(
            env("AOSS_ENDPOINT", DEFAULT_AOSS_ENDPOINT),
            env("AOSS_INDEX", "faq_evidence_a"),
            env("AOSS_SEARCH_PIPELINE", "skywalker-faq-hybrid"),
            region(),
            aossCredentialsProvider());
    }

    @Provides
    @Singleton
    QueryPipeline queryPipeline(
        EmbeddingClient embeddingClient, HybridRetrievalClient retrievalClient, RerankClient rerankClient) {
        return new QueryPipeline(
            embeddingClient,
            retrievalClient,
            rerankClient,
            Integer.parseInt(env("CANDIDATE_BUDGET", "20")),
            Integer.parseInt(env("RERANK_TOP_N", "10")));
    }

    private static AwsCredentialsProvider aossCredentialsProvider() {
        String roleArn = env("AOSS_ASSUME_ROLE_ARN", "");
        if (roleArn.isEmpty()) {
            return DefaultCredentialsProvider.create();
        }
        StsClient sts = StsClient.builder().region(Region.of(region())).build();
        return StsAssumeRoleCredentialsProvider.builder()
            .stsClient(sts)
            .refreshRequest(AssumeRoleRequest.builder()
                .roleArn(roleArn)
                .roleSessionName("skywalker-query")
                .build())
            .build();
    }

    private static String region() {
        return env("AWS_REGION", "us-west-2");
    }

    private static String env(String key, String defaultValue) {
        String value = System.getenv(key);
        return (value == null || value.isBlank()) ? defaultValue : value;
    }
}
