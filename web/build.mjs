// Build the web app: node web/build.mjs
// Bundles src/main.tsx to dist/app.js with esbuild and copies index.html + styles.css.
// Exits non-zero on any error. (Type-checking is `npm run typecheck`, a separate step.)
import { createRequire } from "node:module";
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const esbuild = require("esbuild");
const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "dist");
mkdirSync(dist, { recursive: true });

try {
  await esbuild.build({
    entryPoints: [join(here, "src/main.tsx")],
    bundle: true,
    outfile: join(dist, "app.js"),
    format: "iife",
    jsx: "automatic",
    minify: true,
    sourcemap: true,
    target: ["es2022"],
    define: { "process.env.NODE_ENV": '"production"' },
    nodePaths: (process.env.NODE_PATH || "").split(":").filter(Boolean),
    logLevel: "warning",
  });
  copyFileSync(join(here, "index.html"), join(dist, "index.html"));
  copyFileSync(join(here, "src/styles.css"), join(dist, "styles.css"));
  console.log("built web/dist");
} catch (e) {
  console.error(e.message || e);
  process.exit(1);
}
