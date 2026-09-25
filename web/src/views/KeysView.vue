<script setup>
import { api } from "../api.js";
import { keys } from "../store.js";
import { stamp } from "../format.js";
import { openConfirm, openKeyDialog } from "../ui.js";

function askRevoke(key) {
  openConfirm({
    title: "撤销客户端密钥？",
    desc: `使用「${key.name}」的客户端将无法发起新请求。此操作不可撤销。`,
    submit: () => api("keys/" + key.id, { method: "DELETE" }),
  });
}
</script>

<template>
  <section class="page-panel">
    <div class="banner">API Key 用于客户端调用。密钥仅在创建时完整显示，请及时保存。</div>

    <div class="section-toolbar">
      <div>
        <h2>客户端密钥</h2>
        <p>为不同客户端分别创建密钥，撤销后立即停止接受新请求。</p>
      </div>
      <button class="primary" @click="openKeyDialog">＋ 创建密钥</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>密钥标识</th>
            <th>创建时间</th>
            <th class="align-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="k in keys" :key="k.id">
            <td><strong>{{ k.name }}</strong></td>
            <td><code>{{ k.hint }}</code></td>
            <td class="muted mono">{{ stamp(k.created * 1000) }}</td>
            <td class="align-right">
              <div class="actions">
                <button class="danger" @click="askRevoke(k)">撤销</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
