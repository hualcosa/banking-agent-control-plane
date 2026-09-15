import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * Dev server config for the demo UI.
 *
 * The browser only ever calls relative paths (`/api/threads`, `/api/healthz`),
 * so the agent service never needs CORS middleware — in dev this proxy makes the
 * two same-origin, and in compose nginx does the same job with the same prefix.
 * The `rewrite` strips `/api` because the agent service mounts its routes at the
 * root: it serves `POST /threads`, not `POST /api/threads`.
 *
 * `selfHandleResponse: false` is the default and is what keeps SSE working. The
 * failure mode worth naming: any middleware that reads the proxied body to
 * completion before writing it — a compression layer, a response interceptor, a
 * `buffer` option — turns `POST /threads/{id}/turns/stream` into a single blob
 * that arrives when the turn is already over, and the stage rail then plays its
 * whole animation in one frame. The proxy must forward chunks as they land, so
 * nothing here is allowed to touch the response body.
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
        // http-proxy pipes the upstream response straight through, but it will
        // negotiate gzip on our behalf unless we tell the upstream not to
        // bother. A compressed SSE stream is buffered by the compressor until
        // its window fills, which is indistinguishable from a hung backend.
        headers: { "Accept-Encoding": "identity" },
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
