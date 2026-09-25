<script setup>
import { ref } from "vue";
import { copy } from "../clipboard.js";
import { showToast } from "../store.js";

const baseUrl = location.origin + "/v1";
const anthropicUrl = location.origin;

function copyText(value) {
  return copy(value).then((ok) => showToast(ok ? "已复制" : "无法自动复制，请手动选择文本复制"));
}

const authPath = "%LOCALAPPDATA%\\CodeBuddyExtension\\Data\\Public\\auth";

const endpoints = [
  ["POST", "/v1/chat/completions", "OpenAI Chat Completions"],
  ["POST", "/v1/responses", "Responses 客户端"],
  ["POST", "/v1/messages", "Anthropic Messages"],
  ["GET", "/v1/models", "内置模型列表"],
];

const firstModel = "deepseek-v4-flash";
</script>

<template>
  <section class="page-panel">
    <div class="guide-grid">
      <article class="surface">
        <span class="eyebrow">01 / CREDENTIALS</span>
        <h2>添加登录凭据</h2>
        <p>
          优先使用“添加账号 → 浏览器登录”，在官方页面授权后自动保存，无需桌面客户端。也可在桌面端登录后，按 Win + R 打开以下目录导入文件：
        </p>
        <div class="code-block">
          <code>{{ authPath }}</code>
          <button class="copy-button" @click="copyText(authPath)">复制</button>
        </div>
        <p>
          将其中的 <code>.info</code> 文件拖入“添加账号”，或粘贴其 JSON 内容。支持保留多个账号并手动切换；浏览器授权和文件导入可同时使用。
        </p>
      </article>

      <article class="surface">
        <span class="eyebrow">02 / CLIENTS</span>
        <h2>连接你的客户端</h2>
        <label>OpenAI / Responses Base URL</label>
        <div class="code-block">
          <code>{{ baseUrl }}</code>
          <button class="copy-button" @click="copyText(baseUrl)">复制</button>
        </div>
        <label>Anthropic Base URL</label>
        <div class="code-block">
          <code>{{ anthropicUrl }}</code>
          <button class="copy-button" @click="copyText(anthropicUrl)">复制</button>
        </div>
        <p>
          在“API 密钥”创建 Key 后填入客户端。模型可先使用 <code>{{ firstModel }}</code>；列表中的其他模型是否可用取决于账号权限。
        </p>
      </article>
    </div>

    <article class="surface endpoints">
      <h2>支持的接口</h2>
      <div v-for="[method, path, desc] in endpoints" :key="path">
        <span>{{ method }}</span>
        <code>{{ path }}</code>
        <p>{{ desc }}</p>
      </div>
    </article>
  </section>
</template>
