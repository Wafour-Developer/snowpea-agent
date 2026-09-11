// Bundles the Ink TUI into a single self-contained ESM file that ships as wheel
// package data (§2.5 C1: the user's machine never runs `npm install`).
//
// Because the bundle runs with no node_modules beside it, nothing may stay
// external. Ink's optional `react-devtools-core` import (dev-only, reached only
// when DEV=true) is therefore replaced by an empty stub module rather than
// being marked external.
import { build } from "esbuild";

const optionalDevDeps = ["react-devtools-core"];

const stubOptionalDeps = {
  name: "stub-optional-deps",
  setup(pluginBuild) {
    const filter = new RegExp(`^(${optionalDevDeps.join("|")})$`);
    pluginBuild.onResolve({ filter }, (args) => ({
      path: args.path,
      namespace: "optional-stub",
    }));
    pluginBuild.onLoad({ filter: /.*/, namespace: "optional-stub" }, () => ({
      contents: "export default {};",
      loader: "js",
    }));
  },
};

await build({
  entryPoints: ["src/index.tsx"],
  outfile: "dist/snowpea-tui.js",
  bundle: true,
  platform: "node",
  format: "esm",
  target: "node20",
  jsx: "automatic",
  sourcemap: true,
  plugins: [stubOptionalDeps],
  banner: {
    js: [
      "#!/usr/bin/env node",
      // Parts of Ink's dependency tree still reach for CJS globals under ESM.
      "import { createRequire as __snowpeaCreateRequire } from 'node:module';",
      "const require = __snowpeaCreateRequire(import.meta.url);",
    ].join("\n"),
  },
  external: [],
  logLevel: "info",
});
