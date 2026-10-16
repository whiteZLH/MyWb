<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";
import {
  accounts,
  goPage,
  pool,
  refresh,
  showToast,
  state,
} from "../store.js";
import { number, POOL_STATES, stamp } from "../format.js";
import { openAccount, openConfirm } from "../ui.js";

const query = ref("");
const filter = ref("all");
const batchResult = ref("");
const busyBatch = ref(false);
const form = reactive({ routing: "manual", auto_checkin: false, checkin_time: "09:00", egress_rps: 2, egress_max_wait: 10 });

const filtered = computed(() => {
  const q = query.value.toLowerCase();
  return accounts.value.filter(
    (a) =>
      (!q || [a.name, a.nickname, a.uid].join(" ").toLowerCase().includes(q)) &&
      (filter.value === "all" || a.pool_state === filter.value)
  );
});

const counts = computed(() => {
  const count = (status) => accounts.value.filter((a) => a.pool_state === status).length;
  return `全部 ${accounts.value.length}  ·  可用 ${count("available")}  ·  并发已满 ${count("busy")}  ·  冷却 ${count("cooling")}  ·  暂停 ${count("paused")}  ·  耗尽 ${count("exhausted")}`;
});

const known = computed(() => accounts.value.filter((a) => a.remaining != null));
const totalCredits = computed(() => known.value.reduce((s, a) => s + a.remaining, 0));
const uptimeText = computed(() => {
  const uptime = state.overview?.uptime ?? 0;
  return uptime < 60 ? "已启动不到 1 分钟" : `持续运行 ${Math.floor(uptime / 3600)} 小时 ${Math.floor((uptime % 3600) / 60)} 分钟`;
});
const activeName = computed(() =>
  pool.value.routing === "round_robin" ? "账号池轮转" : accounts.value.find((a) => a.active && a.enabled)?.name || "未选择账号"
);
const activeState = computed(() =>
  pool.value.routing === "round_robin" ? "新请求轮流分配，跳过暂停与冷却账号" : "新请求使用手动指定账号"
);

// 出口 IP 闸：命中上游 WAF 后被判定为 IP 级拦截，期间所有请求快速失败。
const egressBlocked = computed(() => state.overview?.egress_blocked_seconds ?? 0);

const formEl = ref(null);

// 用户在表单内有焦点时不要被轮询刷新覆盖（沿用旧实现的行为）。
watch(
  () => pool.value,
  (value) => {
    if (formEl.value?.contains(document.activeElement)) return;
    form.routing = value.routing || "manual";
    form.auto_checkin = value.auto_checkin || false;
    form.checkin_time = value.checkin_time || "09:00";
    form.egress_rps = value.egress_rps ?? 2;
    form.egress_max_wait = value.egress_max_wait ?? 10;
  },
  { immediate: true, deep: true }
);

function statusOf(account) {
  return POOL_STATES[account.pool_state] || ["待查询", ""];
}

function proxyLabel(account) {
  const value = account.proxy || "";
  if (!value) return "";
  return value.toLowerCase().startsWith("vless://") ? "VLESS 节点" : value;
}

function creditsOf(account) {
  return account.remaining == null ? "—" : number(account.remaining);
}

function openAdd() {
  openAccount();
}

async function batch(action) {
  busyBatch.value = true;
  try {
    const result = await api("pool/actions/" + action, { method: "POST" });
    batchResult.value =
      result.results
        .map((r) => `${accounts.value.find((a) => a.id === r.id)?.name || "账号"}：${r.message}`)
        .join("；") || "没有启用的账号";
    await refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
    busyBatch.value = false;
  }
}

async function act(account, action) {
  try {
    const result = await api(`accounts/${account.id}/actions/${action}`, { method: "POST" });
    showToast(result.message);
    await refresh();
  } catch (error) {
    showToast(error.message);
  }
}

async function uncool(account) {
  try {
    const result = await api(`accounts/${account.id}/actions/uncool`, { method: "POST" });
    showToast(result.message);
    await refresh();
  } catch (error) {
    showToast(error.message);
  }
}

function askProxy(account) {
  openConfirm({
    title: "设置出口代理 / 节点",
    desc: `「${account.name}」的上游出站。留空 = 走全局（CODEBUDDY_EGRESS_PROXY，或全局 VLESS 节点）。支持 http(s)://host:port、socks5(h)://host:port，或直接粘贴 vless:// 分享链接（程序会自动起本地节点，不同账号填不同链接即不同出站 IP）。`,
    text: account.proxy || "",
    textLabel: "代理地址 / VLESS 链接",
    textPlaceholder: "vless://… 或 http://host:port",
    submit: (value) => api("accounts/" + account.id, { method: "PATCH", body: { proxy: value } }),
  });
}

function askNeverCooldown(account) {
  openConfirm({
    title: account.never_cooldown ? "恢复自动冷却？" : "设为永不冷却？",
    desc: account.never_cooldown
      ? `「${account.name}」将恢复按错误自动冷却。`
      : `「${account.name}」遇到 429/403 等错误也不再冷却，会持续参与选号（出口 IP 闸仍可能生效）。`,
    submit: () => api("accounts/" + account.id, { method: "PATCH", body: { never_cooldown: !account.never_cooldown } }),
  });
}

async function saveSettings() {
  try {
    await api("pool/settings", {
      method: "PATCH",
      body: { routing: form.routing, auto_checkin: form.auto_checkin, checkin_time: form.checkin_time, egress_rps: Number(form.egress_rps), egress_max_wait: Number(form.egress_max_wait) },
    });
    await refresh();
    showToast("账号池设置已保存");
  } catch (error) {
    showToast(error.message);
  }
}

function patch(account, body) {
  return () => api("accounts/" + account.id, { method: "PATCH", body });
}

function askActivate(account) {
  openConfirm({
    title: "设置手动 / 测试账号？",
    desc: `设为「${account.name}」。仅在手动调度模式和连接测试中使用；账号池轮转模式仍自动分配。`,
    submit: patch(account, { active: true }),
  });
}

function askRename(account) {
  openConfirm({
    title: "编辑账号备注",
    desc: "备注仅用于在管理后台识别账号。",
    rename: account.name,
    // 备注值由对话框在确认时回传，避免使用打开弹窗时的旧值。
    submit: (value) => api("accounts/" + account.id, { method: "PATCH", body: { name: value } }),
  });
}

function askConcurrency(account) {
  openConfirm({
    title: "设置并发上限",
    desc: `「${account.name}」同时处理的请求数上限；达到上限后新请求会跳过该账号。`,
    number: account.max_concurrent || 0,
    numberLabel: "并发上限",
    submit: (value) => api("accounts/" + account.id, { method: "PATCH", body: { max_concurrent: value } }),
  });
}

function askToggle(account) {
  openConfirm({
    title: account.enabled ? "暂停这个账号？" : "恢复这个账号？",
    desc: "暂停后不参与新请求分配和自动签到，已开始的请求继续完成。恢复后重新参与轮转。",
    submit: patch(account, { enabled: !account.enabled }),
  });
}

function askDelete(account) {
  openConfirm({
    title: "删除这个账号？",
    desc: `「${account.name}」将从账号列表移除。服务器保留恢复副本。${account.active ? "当前调用账号将被清空。" : ""}`,
    submit: () => api("accounts/" + account.id, { method: "DELETE" }),
  });
}
</script>

<template>
  <section class="page-panel">
    <div class="stats">
      <article>
        <span class="stat-label">已添加账号</span>
        <div class="stat-number">{{ accounts.length }}</div>
        <span class="stat-foot">安全保存在你的服务器</span>
      </article>
      <article>
        <span class="stat-label">请求分配</span>
        <div class="stat-value">{{ activeName }}</div>
        <span class="stat-foot">{{ activeState }}</span>
      </article>
      <article class="stat-accent">
        <span class="stat-label">服务状态 <i class="status-dot"></i></span>
        <div class="stat-value">运行中 <span class="spark">↗</span></div>
        <span class="stat-foot">{{ uptimeText }}</span>
      </article>
    </div>

    <div v-if="egressBlocked > 0" class="banner warning" role="alert">
      出口 IP 命中上游 WAF/风控，已暂停调度约 {{ egressBlocked }} 秒。期间新请求会快速失败（避免继续加重风控）；请等待恢复，或考虑为账号配置不同出口。
    </div>

    <div class="pool-summary">
      <span>{{ counts }}</span>
      <strong>
        已知总积分 {{ known.length ? number(totalCredits) : "待查询" }}
        <template v-if="known.length && known.length < accounts.length">（部分）</template>
      </strong>
    </div>

    <div class="section-toolbar">
      <div>
        <h2>账号列表 <span class="count-badge">{{ accounts.length }}</span></h2>
        <p>暂停账号不参与轮转；余额按最近一次查询结果显示。</p>
      </div>
      <button class="primary" @click="openAdd">＋ 添加账号</button>
    </div>

    <div class="pool-toolbar">
      <input v-model="query" type="search" placeholder="搜索备注、昵称或 UID" aria-label="搜索账号" />
      <select v-model="filter" aria-label="筛选账号状态">
        <option value="all">全部状态</option>
        <option value="available">可用</option>
        <option value="busy">并发已满</option>
        <option value="cooling">冷却中</option>
        <option value="paused">已暂停</option>
        <option value="exhausted">积分耗尽</option>
        <option value="invalid">凭据异常</option>
      </select>
      <button class="secondary" :disabled="busyBatch" @click="batch('checkin')">▣ 批量签到</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>账号</th>
            <th>状态 / 签到</th>
            <th>并发</th>
            <th>剩余积分</th>
            <th>凭据有效期</th>
            <th class="align-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in filtered" :key="a.id">
            <td>
              <div class="account-cell">
                <span class="account-icon">{{ (a.name || "?").slice(0, 1) }}</span>
                <div>
                  <strong>
                    {{ a.name }}
                    <span v-if="a.active" class="mini-active">手动 / 测试账号</span>
                  </strong>
                  <small>{{ a.uid || a.nickname }}</small>
                  <small v-if="a.proxy" class="cell-note">代理 {{ proxyLabel(a) }}</small>
                </div>
              </div>
            </td>
            <td>
              <span class="pill" :class="statusOf(a)[1]">{{ statusOf(a)[0] }}</span>
              <small class="cell-note">{{ a.today_checked_in ? "今日已签到" : "今日未确认签到" }}</small>
              <small v-if="a.never_cooldown" class="cell-note">永不冷却</small>
              <small v-if="a.cooldown_until > Date.now() / 1000" class="cell-note">
                至 {{ stamp(a.cooldown_until * 1000) }}
              </small>
            </td>
            <td>
              <strong class="credit-number">
                {{ a.inflight ?? 0 }}<template v-if="a.max_concurrent"> / {{ a.max_concurrent }}</template>
              </strong>
              <small class="cell-note">{{ a.max_concurrent ? "在途 / 上限" : "不限并发" }}</small>
            </td>
            <td>
              <strong class="credit-number">{{ creditsOf(a) }}</strong>
              <small class="cell-note">
                {{ a.credits_updated ? stamp(a.credits_updated * 1000) : "点击查询积分" }}{{ a.credits_stale && a.credits_updated ? " · 待刷新" : "" }}
              </small>
              <small v-if="a.last_error" class="cell-note field-error">{{ a.last_error }}</small>
            </td>
            <td class="mono">
              {{ stamp(a.expires_at) }}
              <small class="cell-note">{{ a.expired ? "已到期 · 调用时尝试刷新" : "支持自动刷新" }}</small>
            </td>
            <td>
              <div class="actions pool-actions">
                <template v-if="a.enabled">
                  <button title="查询积分与签到状态" @click="act(a, 'status')">查询积分</button>
                  <button @click="act(a, 'checkin')">签到</button>
                  <button @click="act(a, 'refresh')">刷新凭据</button>
                </template>
                <button v-if="a.cooldown_until > Date.now() / 1000" @click="uncool(a)">解除冷却</button>
                <button class="switch" v-if="a.enabled && !a.active" @click="askActivate(a)">设为手动 / 测试</button>
                <button @click="askRename(a)">备注</button>
                <button @click="askConcurrency(a)">并发</button>
                <button @click="askProxy(a)">代理</button>
                <button @click="askNeverCooldown(a)">{{ a.never_cooldown ? "恢复冷却" : "永不冷却" }}</button>
                <button @click="askToggle(a)">{{ a.enabled ? "暂停" : "恢复" }}</button>
                <button class="danger" @click="askDelete(a)">删除</button>
              </div>
            </td>
          </tr>
          <tr v-if="!filtered.length && accounts.length">
            <td colspan="6" class="muted">没有符合筛选条件的账号。</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!accounts.length" class="empty">
        <span class="empty-icon">◫</span>
        <h3>从第一个账号开始</h3>
        <p>浏览器授权或导入登录文件，即可加入账号池。</p>
        <button class="secondary" @click="openAdd">添加登录凭据</button>
      </div>
    </div>

    <div v-if="batchResult" class="banner pool-result" role="status">{{ batchResult }}</div>

    <form ref="formEl" class="pool-settings" @submit.prevent="saveSettings">
      <div>
        <h3>自动签到 · 轮转 · 出口限速</h3>
        <p>北京时间每日签到；错过执行时间会补查，已签到不重复领取。页面每 30 秒刷新，服务器每 5 分钟查询积分。出口速率上限是同一出口 IP 上全部上游请求的聚合上限，用于避免触发上游 WAF/429；0 表示关闭。</p>
      </div>
      <div class="pool-controls">
        <label>
          请求分配
          <select v-model="form.routing">
            <option value="round_robin">账号池轮转</option>
            <option value="manual">手动指定账号</option>
          </select>
        </label>
        <label>签到时间（北京时间）<input v-model="form.checkin_time" type="time" required /></label>
        <label class="inline-checkbox"><input v-model="form.auto_checkin" type="checkbox" />每日自动签到</label>
        <label>
          出口速率上限（req/s）
          <input v-model.number="form.egress_rps" type="number" min="0" max="50" step="0.5" />
        </label>
        <label>
          排队等待上限（秒）
          <input v-model.number="form.egress_max_wait" type="number" min="0" max="120" step="1" />
        </label>
        <button class="primary" type="submit">保存设置</button>
      </div>
    </form>

    <div class="note-row">
      <span>ⓘ</span>
      <p>访问令牌过期时，服务会尝试自动刷新。刷新凭据也失效后，请在桌面端重新登录并导入文件。页面中的“可刷新”不代表已验证账号额度。</p>
    </div>

    <div class="bottom-guide">
      <div>
        <span class="eyebrow">NEXT STEP</span>
        <h3>账号准备好，连接你的客户端。</h3>
        <p>兼容 OpenAI Chat、Responses 和 Anthropic Messages。</p>
      </div>
      <button class="text-button" @click="goPage('guide')">查看接入指南 ↗</button>
    </div>
  </section>
</template>
