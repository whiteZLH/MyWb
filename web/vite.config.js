import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 构建产物直接落到后端的静态目录（admin/static），
// 由 FastAPI 在 /admin/ 与 /admin/assets/* 路由提供。
//
// 关键约束：后端 CSP 为 script-src 'self'（不允许内联脚本与 eval），
// 因此构建产物里不能包含任何内联 <script>；Vite 默认会把入口拆成
// 独立文件，这里只固定产物文件名。
//
// 开发调试：`npm run dev` 时把 /admin/api 反代到本地后端。
// 后端地址取值顺序：MYWB_ADMIN_URL > 项目 .env 的 PORT > 8787。
const root = dirname(fileURLToPath(import.meta.url));

function backendPort() {
  try {
    const text = readFileSync(resolve(root, "../.env"), "utf-8");
    const match = text.match(/^\s*(?:export\s+)?PORT\s*=\s*["']?(\d+)["']?\s*$/m);
    if (match) return match[1];
  } catch {
    // 没有 .env 时回落到默认端口。
  }
  return "8787";
}

const backend = process.env.MYWB_ADMIN_URL || `http://127.0.0.1:${backendPort()}`;

export default defineConfig(({ command }) => ({
  plugins: [vue()],
  // 仅构建产物需要挂在 /admin/assets/ 下由后端提供；
  // dev 用根路径，避免出现 http://localhost:5173/admin/assets/ 这种地址。
  base: command === "build" ? "/admin/assets/" : "/",
  server: {
    proxy: {
      // 只代理后端 API；/admin/assets/* 仍由 Vite 自己提供（HMR 资源）。
      "/admin/api": { target: backend, changeOrigin: true, ws: false },
    },
  },
  build: {
    outDir: "../admin/static",
    emptyOutDir: true,
    assetsDir: ".",
    // 关闭 modulepreload 的 polyfill 注入，避免违反 CSP。
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        // 固定文件名，避免需要枚举 / 静态目录挂载点。
        entryFileNames: "app.js",
        assetFileNames: "[name][extname]",
        chunkFileNames: "chunk-[name].js",
      },
    },
  },
}));
