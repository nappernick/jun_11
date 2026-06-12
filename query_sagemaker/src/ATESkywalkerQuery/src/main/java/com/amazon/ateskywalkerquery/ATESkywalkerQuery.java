package com.amazon.ateskywalkerquery;

import amazon.platform.config.AppConfig;
import com.amazon.ateskywalkerquery.module.ATESkywalkerQueryModule;
import com.amazon.ateskywalkerquery.module.CoralModule;
import com.amazon.coral.bobcat.BobcatServer;
import com.amazon.coral.metrics.MetricsFactory;
import com.amazon.coral.metrics.emf.EmfWriter;
import com.amazon.coral.service.EnvironmentChecker;
import com.google.inject.Injector;
import com.google.inject.Stage;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.Logger;

import java.util.Arrays;

import static com.google.inject.Guice.createInjector;

import static java.lang.Runtime.getRuntime;

public final class ATESkywalkerQuery {

    private static final Logger LOG = LogManager.getLogger(ATESkywalkerQuery.class);
    private static final String APP_NAME = "ATESkywalkerQuery";

    private ATESkywalkerQuery() {}

    public static void main(String[] args) throws Exception {
        LOG.info("Starting with args {}", Arrays.toString(args));
        initAppConfig(args);

        String root = System.getProperty("root");
        String realm = AppConfig.getRealm().name();
        String domain = AppConfig.getDomain();

        Injector injector;
        try {
            injector =
                createInjector(Stage.PRODUCTION, new CoralModule(root, domain, realm), new ATESkywalkerQueryModule());
        } catch (Exception e) {
            LOG.error("Exception while creating injector: ", e);
            LogManager.shutdown();
            System.exit(1);
            return;
        }
        checkEnvironment(injector);
        BobcatServer server = injector.getInstance(BobcatServer.class);
        EmfWriter writer = injector.getInstance(EmfWriter.class);

        getRuntime().addShutdownHook(new Thread(() -> {
            server.shutdown();
            writer.close();
            LogManager.shutdown();
        }));
        server.start();

        // wait for termination
        Thread.currentThread().join();
    }

    private static void checkEnvironment(Injector injector) {
        new EnvironmentChecker(injector.getInstance(MetricsFactory.class));
    }

    private static void initAppConfig(String[] args) {
        verifyArguments(args);

        AppConfig.initialize(APP_NAME, null, args);
    }

    private static void verifyArguments(String[] args) {
        var hasRealm = false;
        var hasDomain = false;
        var hasRoot = false;

        for (String arg : args) {
            if (arg.startsWith("--realm=")) {
                hasRealm = true;
            } else if (arg.startsWith("--domain=")) {
                hasDomain = true;
            } else if (arg.startsWith("--root=")) {
                hasRoot = true;
            }
        }

        if (!(hasRealm && hasDomain && hasRoot)) {
            LOG.error(
                """
               The service cannot determine what environment it is running in and will shut down.
               If you are trying to run from a local workspace, add the following to your launch configuration

                   --domain=test --realm=us-west-2 --root=build/private

               If you're trying seeing this on a deployed host, the initiation script has not passed the appropriate
               command line parameters to the Java program.
               """);
            System.exit(2);
        }
    }
}
