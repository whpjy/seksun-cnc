import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readdir, readFile } from "node:fs/promises";

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
    plugins: [react(), {
      name: "pdf-viewer-resources",
      async generateBundle() {
        const root = new URL("./node_modules/pdfjs-dist/", import.meta.url);
        for (const directory of ["cmaps", "standard_fonts", "wasm"]) {
          for (const entry of await readdir(new URL(`${directory}/`, root), { withFileTypes: true })) {
            if (entry.isFile()) this.emitFile({ type: "asset", fileName: `pdfjs/${directory}/${entry.name}`, source: await readFile(new URL(`${directory}/${entry.name}`, root)) });
          }
        }
      },
      configureServer(server) {
        server.middlewares.use("/pdfjs", async (req, res, next) => {
          const relative = (req.url || "").split("?")[0];
          if (!/^\/(cmaps|standard_fonts|wasm)\/[a-zA-Z0-9_.-]+$/.test(relative)) return next();
          try { res.end(await readFile(new URL(`./node_modules/pdfjs-dist${relative}`, import.meta.url))); } catch { next(); }
        });
      },
    }, { name: "process-studio-entry", configureServer(server) {
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
