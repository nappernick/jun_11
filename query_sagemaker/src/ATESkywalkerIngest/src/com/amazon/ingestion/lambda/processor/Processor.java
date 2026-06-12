package com.amazon.ingestion.lambda.processor;

import com.amazonaws.services.lambda.runtime.Context;
import com.amazonaws.services.lambda.runtime.RequestHandler;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.Map;

public class Processor implements RequestHandler<Map<String, Object>, String> {

    private static final Logger LOGGER = LogManager.getLogger(Processor.class);

    @Override
    public String handleRequest(Map<String, Object> event, Context context) {
        LOGGER.info("Processor invoked");
        return "processor placeholder";
    }
}
