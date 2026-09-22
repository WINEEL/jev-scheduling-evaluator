import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  /**
   * Task 76: build a self-contained server bundle for the container image.
   *
   * `standalone` traces the files the server actually imports and writes them,
   * with a minimal `node_modules`, into `.next/standalone`. The Dockerfile
   * copies that instead of the whole dependency tree, which is the difference
   * between an image of a few hundred megabytes and one well over a gigabyte --
   * and on Cloud Run with minimum instances 0, image size is cold-start time
   * that somebody is waiting through.
   *
   * It changes nothing about how the app runs locally: `next dev` and
   * `next start` ignore it.
   */
  output: "standalone",
};

export default nextConfig;
