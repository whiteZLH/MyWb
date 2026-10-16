<script setup>
import { computed, ref } from "vue";
import { api } from "../api.js";
import { nodes, refresh, showToast, vless } from "../store.js";
import { openConfirm } from "../ui.js";

const text = ref("");
const busy = ref(false);
const probing = ref(false);

const enabledCount = computed(() => nodes.value.filter((n) => n.enabled).length);
const healthyCount = computed(() => nodes.value.filter((n) => n.health && n.health.ok).length);

async function addNodes() {
  const payload = text.value.trim();
  if (!payload) {
    showToast("请粘贴 vless:// 链接，每行一条");
    return;
  }
  busy.value = true;
  try {
    const result = await api("nodes", { method: "POST", body: { text: payload } });
    showToast(`已添加 ${result.added} 个，跳过 ${result.skipped} 个`);
    text.value = "";
    await refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    busy.value = false;
  }
}

async function probeAll() {
  probing.value = true;
  try {
    const result = await api("nodes/probe", { method: "POST", body: {} });
    const ok = result.results.filter((item) => item.ok).length;
    showToast(`探测完成：可用 ${ok}/${result.results.length}`);
    await refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    probing.value = false;
  }
}

async function probeOne(node) {
  try {
    const result = await api("nodes/probe", { method: "POST", body: { id: node.id } });
    const item = result.results[0];
    showToast(item && item.ok ? `可用 · ${item.ip} · ${item.ms}ms` : `不可用：${(item && item.error) || "未知"}`);
    await refresh();
  } catch (error) {
    showToast(error.message);
  }
}

async function assign(mode) {
  try {
    const result = await api("nodes/assign", { method: "POST", body: { mode } });
    showToast(mode === "clear" ? "已清除账号的节点分配" : `已分配 ${result.assigned} 个账号，使用 ${result.nodes} 个节点`);
    await refresh();
  } catch (error) {
    showToast(error.message);
  }
}

async function toggle(node) {
  try {
    await api("nodes/" + node.id, { method: "PATCH", body: { enabled: !node.enabled } });
    await refresh();
  } catch (error) {
    showToast(error.message);
  }
}

function askRemark(node) {
  openConfirm({
    title: "节点备注",
    desc: "仅用于在面板识别该节点。",
    text: node.remark || "",
    textLabel: "备注",
    submit: (value) => api("nodes/" + node.id, { method: "PATCH", body: { remark: value } }),
  });
}

function askDelete(node) {
  openConfirm({
    title: "删除节点？",
    desc: `「${node.remark || node.label}」将从节点池移除；已分配它的账号需要重新分配。`,
    submit: () => api("nodes/" + node.id, { method: "DELETE" }),
  });
}

function healthOf(node) {
  const health = node.health;
  if (!health) return { label: "未探测", cls: "" };
  if (health.ok) return { label: `${health.ip || "可用"} · ${health.ms}ms`, cls: "green" };
  return { label: health.error || "不可用", cls: "red" };
}
</script>

<template>
  <section class="page-panel">
    <div class="stats">
      <article>
        <span class="stat-label">节点总数</span>
        <div class="stat-number">{{ nodes.length }}</div>
        <span class="stat-foot">启用 {{ enabledCount }} 个</span>
      </article>
      <article>
        <span class="stat-label">已探测可用</span>
        <div class="stat-value">{{ healthyCount }} / {{ nodes.length }}</div>
        <span class="stat-foot">探测结果仅存于内存，重启后重测</span>
      </article>
      <article class="stat-accent">
        <span class="stat-label">内置内核 <i class="status-dot"></i></span>
        <div class="stat-value">
          {{ vless.running ? "运行中" : "未运行" }} <span class="spark">⇄</span>
        </div>
        <span class="stat-foot">{{ vless.error || "sing-box 由程序按需拉起" }}</span>
      </article>
    </div>

    <div v-if="vless.error" class="banner warning" role="alert">内核问题：{{ vless.error }}</div>

    <form class="pool-settings" @submit.prevent="addNodes">
      <div>
        <h3>批量添加节点</h3>
        <p>每行一条 <code>vless://</code> 链接（支持 ws/tls/reality 等参数）。重复或非法行会被跳过。不同账号分配不同节点即可获得不同出站 IP。</p>
      </div>
      <textarea v-model="text" rows="5" placeholder="vless://…&#10;vless://…" spellcheck="false"></textarea>
      <div class="pool-controls">
        <button class="primary" type="submit" :disabled="busy">添加</button>
      </div>
    </form>

    <div class="section-toolbar">
      <div>
        <h2>节点列表 <span class="count-badge">{{ nodes.length }}</span></h2>
        <p>探测通过的节点优先用于自动分配。</p>
      </div>
      <div class="actions">
        <button class="secondary" :disabled="probing || !nodes.length" @click="probeAll">
          {{ probing ? "探测中…" : "探测全部" }}
        </button>
        <button class="secondary" :disabled="!nodes.length" @click="assign('round_robin')">自动分配到账号</button>
        <button class="secondary" @click="assign('clear')">清除分配</button>
      </div>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>节点</th>
            <th>状态</th>
            <th>本地端口</th>
            <th class="align-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="node in nodes" :key="node.id">
            <td>
              <strong>{{ node.remark || node.label }}</strong>
              <small class="cell-note mono">{{ node.label }}</small>
            </td>
            <td>
              <span class="pill" :class="healthOf(node).cls">{{ healthOf(node).label }}</span>
              <small v-if="!node.enabled" class="cell-note">已禁用</small>
            </td>
            <td class="mono">{{ node.port || "—" }}</td>
            <td>
              <div class="actions pool-actions">
                <button @click="probeOne(node)">探测</button>
                <button @click="askRemark(node)">备注</button>
                <button @click="toggle(node)">{{ node.enabled ? "禁用" : "启用" }}</button>
                <button class="danger" @click="askDelete(node)">删除</button>
              </div>
            </td>
          </tr>
          <tr v-if="!nodes.length">
            <td colspan="4" class="muted">节点池为空。粘贴一条或多条 vless:// 链接开始。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="note-row">
      <span>ⓘ</span>
      <p>「自动分配到账号」会按顺序把可用节点轮流写入各启用账号的代理（覆盖原有代理）。想固定某个账号走某节点，请到「账号池」页点该账号的「代理」手动指定。</p>
    </div>
  </section>
</template>
