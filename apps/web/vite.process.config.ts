import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Separate server and build directory: the original Harness and CNC entries are untouched.
export default defineConfig(async () => {
  const harness = process.env.PROCESS_HARNESS_TARGET || "http://127.0.0.1:3081";
  const cnc = process.env.PROCESS_CNC_TARGET || "http://127.0.0.1:3001";
  let cookie = "";
  if (process.env.PROCESS_HARNESS_TOKEN) {
    const login = new URL("/", harness);
    login.searchParams.set("token", process.env.PROCESS_HARNESS_TOKEN);
    const response = await fetch(login, { redirect: "manual" });
    if (response.status !== 303) throw new Error("Harness authentication failed; obtain its current launch token.");
    cookie = (response.headers.get("set-cookie") || "").split(";", 1)[0];
    if (!cookie) throw new Error("Harness did not issue a session cookie.");
  }
  return {
    plugins: [react(), { name: "process-studio-entry", configureServer(server) {
      server.middlewares.use((req, _res, next) => { if (req.url === "/") req.url = "/process.html"; next(); });
    } }],
    server: {
      host: "127.0.0.1", port: 3082, strictPort: true,
      proxy: {
        "/harness": {
          target: harness, changeOrigin: true, ws: true,
          rewrite: (path) => path.replace(/^\/harness/, ""),
          configure(proxy) {
            const headers = (request: { setHeader(name: string, value: string): void; removeHeader(name: string): void }) => {
              // Authentication stays on this local server; never put the launch token in a client bundle.
              if (cookie) request.setHeader("cookie", cookie);
              request.setHeader("origin", new URL(harness).origin);
              request.removeHeader("referer");
            };
            proxy.on("proxyReq", headers);
            proxy.on("proxyReqWs", headers);
          },
        },
        "/api": { target: cnc, changeOrigin: true },
      },
    },
    build: { outDir: "dist/process-studio", rollupOptions: { input: "process.html" } },
  };
});
