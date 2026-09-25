import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 构建产物直接输出到后端的静态目录（admin/static），
// 由 FastAPI 的 /admin/ 与 /admin/assets/* 路由提供服务。
//
// 关键约束：后端 CSP 为 script-src 'self'，不允许内联脚本与 eval，
// 因此构建产物不能包含任何内联 <script>。Vite 默认会把入口脚本作为
// 独立文件输出，这里只需确保不做内联即可。
export default defineConfig({
  plugins: [vue()],
  base: "/admin/assets/",
  build: {
    outDir: "../admin/static",
    emptyOutDir: true,
    assetsDir: ".",
    // 关闭 modulepreload 的内联 polyfill 注入，避免违反 CSP。
    modulePreload: { polyfill: false },
    rollupOptions: {
      output: {
        // 固定文件名，避免后端需要枚举 / 动态放行哈希文件。
        entryFileNames: "app.js",
        assetFileNames: "[name][extname]",
        chunkFileNames: "chunk-[name].js",
      },
    },
  },
});
