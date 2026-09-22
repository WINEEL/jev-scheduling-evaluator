import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

/**
 * Vitest covers the parts of this app that can be reasoned about without a
 * browser: the API client, the development-auth guard, the proxy handler, and
 * the pure helpers that decide what each screen may show.
 *
 * No jsdom and no component renderer, deliberately. The rules worth protecting
 * -- who may start a schedule, how many positions are unfilled, what a
 * development environment is allowed to send -- were written as plain
 * functions precisely so they could be tested directly.
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
