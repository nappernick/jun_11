
plugins {
    id("smithy-model-package-plugin")
}

smithy {
    tags = setOf("ATESkywalkerQuery")
}

// not strictly necessary for build, but helps IntelliJ plugin
// load the smithy source into the project properly
java.sourceSets["main"].java {
    srcDirs("model")
}

tasks.compileJava {
    options.release = 8
}
