@CoralGenerate(
    explorer = @Explorer,
    index = @Index(name = "GeneratedModelIndexFactory"),
    models = @Models("ATESkywalkerQueryModel"),
    modelValidation = @ModelValidation(ModelValidation.Basic),
    server = @Server(interfaces = true),
    types = @Types)
package com.amazon.ateskywalkerquery;

import com.amazon.coral.annotation.generator.CoralGenerate;
import com.amazon.coral.annotation.generator.Explorer;
import com.amazon.coral.annotation.generator.Index;
import com.amazon.coral.annotation.generator.ModelValidation;
import com.amazon.coral.annotation.generator.Models;
import com.amazon.coral.annotation.generator.Server;
import com.amazon.coral.annotation.generator.Types;
