<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { loadRequests, showToast, state } from "../store.js";
import { number, OUTCOMES, seconds, stamp, tokensPerSecond } from "../format.js";

const filter = ref("all");
const query = ref("");

const rows = computed(() => {
  const q = query.value.trim().toLowerCase();
  return (state.requests ?? []).filter(
    (r) =>
      (filter.value === "all" || (filter.value === "stream" ? r.streaming : !r.streaming)) &&
      (!q || [r.key, r.account, r.model, r.path, OUTCOMES[r.outcome]].join(" ").toLowerCase().includes(q))
  );
});

const sourceText = (r) => (r.source === "test" ? "后台测试" : "API");

function tokens(r) {
  const t = r.tokens;
  if (!t) return "—";
  return number(t.total);
}

function tokenNote(r) {
  const t = r.tokens;
  if (!t) return "";
  const parts = [`输入 ${number(t.prompt)}`, `输出 ${number(t.completion)}`];
  if (t.cached) parts.push(`缓存 ${number(t.cached)}`);
  if (t.reasoning) parts.push(`思考 ${number(t.reasoning)}`);
  return parts.join(" · ");
}

let timer = null;
onMounted(() => {
  loadRequests().catch((e) => showToast(e.message));
  timer = setInterval(() => {
    if (!document.hidden) loadRequests().catch(() => {});
  }, 15000);
});
onBeforeUnmount(() => clearInterval(timer));
</script>

<template>
  <section class="page-panel">
    <div class="banner">
      记录最近 100 次 API 调用，仅保留时间、处理账号、状态、耗时与 Token 计数；不记录提示词、回复内容或完整密钥。记录写入本地数据库，重启后仍可查看。
    </div>

    <div class="section-toolbar">
      <div>
        <h2>请求记录 <span class="count-badge">{{ rows.length }}</span></h2>
        <p>每条记录对应用户端发起的一次调用，含流式与非流式请求。</p>
      </div>
    </div>

    <div class="pool-toolbar">
      <input v-model="query" type="search" placeholder="搜索账号、密钥、模型或接口" aria-label="搜索请求记录" />
      <select v-model="filter" aria-label="筛选请求类型">
        <option value="all">全部类型</option>
        <option value="stream">仅流式</option>
        <option value="nonstream">仅非流式</option>
      </select>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>账号</th>
            <th>密钥 / 来源</th>
            <th>模型 / 接口</th>
            <th>类型</th>
            <th>首字延迟</th>
            <th>总耗时</th>
            <th>生成速度</th>
            <th>Token 消耗</th>
            <th class="align-right">状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="i">
            <td class="mono muted">{{ stamp(r.time * 1000) }}</td>
            <td>
              <strong>{{ r.account || "—" }}</strong>
              <small v-if="!r.account" class="cell-note">未识别账号</small>
            </td>
            <td>
              <strong>{{ r.key || "未识别密钥" }}</strong>
              <small class="cell-note">{{ sourceText(r) }}</small>
            </td>
            <td>
              {{ r.model || "—" }}
              <small class="cell-note mono">{{ r.path }}</small>
            </td>
            <td>
              <span class="pill" :class="r.streaming ? 'amber' : ''">{{ r.streaming ? "流式" : "非流式" }}</span>
            </td>
            <td class="mono">{{ r.ttft_ms == null ? "—" : number(r.ttft_ms) + " ms" }}</td>
            <td class="mono">{{ number(r.duration_ms) }} ms</td>
            <td class="mono">
              <template v-if="tokensPerSecond(r) == null">—</template>
              <template v-else>
                {{ tokensPerSecond(r).toFixed(1) }}
                <small class="cell-note">tok/s</small>
              </template>
            </td>
            <td class="mono">
              {{ tokens(r) }}
              <small v-if="r.tokens" class="cell-note">{{ tokenNote(r) }}</small>
            </td>
            <td class="align-right">
              <span class="pill" :class="r.ok ? 'green' : 'red'">{{ r.status ?? "无响应" }}</span>
              <small class="cell-note">{{ OUTCOMES[r.outcome] || "请求失败" }}</small>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!rows.length" class="empty">
        <span class="empty-icon">◷</span>
        <h3>{{ state.requests?.length ? "没有符合筛选条件的记录" : "还没有请求记录" }}</h3>
        <p>发起一次 API 调用或连接测试后，这里会逐条展示首字延迟、耗时与 Token 消耗。</p>
      </div>
    </div>

    <div class="note-row">
      <span>ⓘ</span>
      <p>首字延迟与生成速度仅对流式请求有效；非流式响应整包返回，无法拆分首字时刻与解码时长，因此显示为“—”。生成速度按输出 tokens ÷ 解码时长计算。仅保留最近 100 条记录。</p>
    </div>
  </section>
</template>
