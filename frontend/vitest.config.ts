import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

/**
 * Vitest covers the parts of this app that can be reasoned about without a
 * browser: the transport boundary, the presentation rules, and the proxy
 * handler.
 *
 * No jsdom and no component renderer, deliberately. Every rule worth
 * protecting — how a probability is read, which threshold a reason was
 * measured against, what the proxy refuses to forward — was written as a plain
 * function precisely so it could be tested directly.
 */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
