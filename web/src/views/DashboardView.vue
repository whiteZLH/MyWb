<script setup>
import { computed } from "vue";
import { copy } from "../clipboard.js";
import {
  accounts,
  events,
  goPage,
  metrics,
  pool,
  showToast,
  state,
} from "../store.js";
import { number, OUTCOMES, POOL_STATES, rate, stamp } from "../format.js";
import { openAccount } from "../ui.js";

const base = location.origin + "/v1";

const available = computed(() => accounts.value.filter((a) => a.pool_state === "available").length);
const known = computed(() => accounts.value.filter((a) => typeof a.remaining === "number"));
const totalCredits = computed(() => known.value.reduce((sum, a) => sum + a.remaining, 0));
const creditNote = computed(() => {
  if (known.value.length < accounts.value.length) {
    return `已查询 ${known.value.length}/${accounts.value.length} 个账号，余额可能不完整`;
  }
  return accounts.value.some((a) => a.credits_stale) ? "包含待刷新余额，以最近一次查询为准" : "以最近一次上游查询为准";
});
const routing = computed(() =>
  pool.value.routing === "round_robin" ? "轮流分配请求，自动跳过不可用账号" : "手动指定账号模式"
);
const recent = computed(() => metrics.value.recent ?? []);

async function copyBase() {
  showToast((await copy(base)) ? "已复制" : "无法自动复制，请手动选择文本复制");
}
</script>

<template>
  <section class="page-panel">
    <div class="dashboard-metrics">
      <article>
        <span>可用账号</span>
        <strong>{{ available }} / {{ accounts.length }}</strong>
        <small>启用且未冷却、未确认耗尽</small>
      </article>
      <article>
        <span>账号总积分</span>
        <strong>{{ known.length ? number(totalCredits) : "—" }}</strong>
        <small>{{ creditNote }}</small>
      </article>
      <article>
        <span>对话请求</span>
        <strong>{{ number(metrics.completed || 0) }}</strong>
        <small>API {{ metrics.api_count || 0 }} · 后台测试 {{ metrics.test_count || 0 }}</small>
      </article>
      <article>
        <span>完成成功率</span>
        <strong>{{ rate(metrics.success_rate) }}</strong>
        <small>排除流式错误及未完整结束的请求</small>
      </article>
    </div>

    <div class="dashboard-detail">
      <span>HTTP 成功率 <b>{{ rate(metrics.http_success_rate) }}</b></span>
      <span>失败 <b>{{ metrics.failed || 0 }}</b></span>
      <span>进行中 <b>{{ metrics.in_flight || 0 }}</b></span>
      <span>平均耗时 <b>{{ metrics.avg_duration_ms == null ? "—" : number(metrics.avg_duration_ms) + " ms" }}</b></span>
      <button class="primary" @click="openAccount()">＋ 添加账号</button>
    </div>

    <div class="dashboard-grid">
      <div>
        <article class="surface dash-accounts">
          <div class="response-header">
            <div>
              <h2>账号工作区</h2>
              <p class="muted">{{ routing }}</p>
            </div>
            <button class="text-button" @click="goPage('accounts')">管理账号 ↗</button>
          </div>
          <div>
            <div v-for="a in accounts.slice(0, 8)" :key="a.id" class="dashboard-account">
              <span class="account-icon">{{ (a.name || "?").slice(0, 1) }}</span>
              <div>
                <strong>{{ a.name }}</strong>
                <small class="cell-note">{{ a.uid || a.nickname }}</small>
              </div>
              <div class="dashboard-account-credit">
                <strong>{{ a.remaining == null ? "待查询" : number(a.remaining) }}</strong>
                <small class="cell-note">积分</small>
              </div>
              <span class="pill" :class="a.pool_state === 'available' ? 'green' : 'amber'">
                {{ (POOL_STATES[a.pool_state] || ["待查询"])[0] }}
              </span>
            </div>
            <p v-if="!accounts.length" class="history-empty">尚未添加账号，点击“添加账号”开始。</p>
            <p v-if="accounts.length > 8" class="muted">更多账号请前往账号池查看。</p>
          </div>
        </article>

        <article class="surface dash-recent">
          <div class="response-header">
            <div>
              <h2>最近请求</h2>
              <p class="muted">最近 100 条，仅记录时间、来源、接口、状态和耗时。</p>
            </div>
            <span class="eyebrow">LIVE ACTIVITY</span>
          </div>
          <div class="dashboard-table">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>来源 / 接口</th>
                  <th>响应状态</th>
                  <th class="align-right">耗时</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, i) in recent" :key="i">
                  <td class="mono muted">{{ stamp(r.time * 1000) }}</td>
                  <td>
                    {{ r.source === "test" ? "后台测试" : "API" }}
                    <small class="cell-note mono">{{ r.path }}</small>
                  </td>
                  <td>
                    <span class="pill" :class="r.ok ? 'green' : 'red'">{{ r.status ?? "—" }}</span>
                    <small class="cell-note">{{ OUTCOMES[r.outcome] || "请求失败" }}</small>
                  </td>
                  <td class="align-right mono">{{ number(r.duration_ms) }} ms</td>
                </tr>
                <tr v-if="!recent.length">
                  <td colspan="4" class="history-empty">
                    尚无请求记录。发起 API 调用或后台测试后，这里会自动更新。
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </article>
      </div>

      <aside>
        <article class="dashboard-connect">
          <span class="eyebrow light">API CONNECTION</span>
          <h2>随时接入，<br />你熟悉的工具。</h2>
          <p>一个入口，连接你的 WorkBuddy 模型。</p>
          <label>BASE URL</label>
          <code>{{ base }}</code>
          <button class="secondary" @click="copyBase">复制 API 地址</button>
          <button class="dashboard-link" @click="goPage('guide')">查看接入指南 ↗</button>
        </article>
        <article class="dash-start">
          <h3>开始使用</h3>
          <button @click="goPage('accounts')">① 管理账号与积分 ↗</button>
          <button @click="goPage('test')">② 测试模型响应 ↗</button>
          <button @click="goPage('keys')">③ 管理客户端密钥 ↗</button>
          <p class="muted">统计开始于 {{ stamp((metrics.started_at || 0) * 1000) }}</p>
          <p class="muted">统计自本次进程启动起累计，重启后清零；历史按天聚合在运行统计页查看。</p>
        </article>
      </aside>
    </div>
  </section>
</template>
