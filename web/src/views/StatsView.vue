<script setup>
import { computed, onBeforeUnmount, onMounted } from "vue";
import { loadStats, showToast, state } from "../store.js";
import { number, rate, seconds } from "../format.js";

const data = computed(() => state.stats?.[state.statsRange] ?? {});
const rows = computed(() => data.value.models ?? []);
const accountRows = computed(() => data.value.accounts ?? []);

function setRange(range) {
  state.statsRange = range;
}

let timer = null;
onMounted(() => {
  loadStats().catch((e) => showToast(e.message));
  timer = setInterval(() => {
    if (!document.hidden) loadStats().catch(() => {});
  }, 30000);
});
onBeforeUnmount(() => clearInterval(timer));
</script>

<template>
  <section class="page-panel">
    <div class="stats-range" role="group" aria-label="统计范围">
      <button class="range-button" :class="{ selected: state.statsRange === 'today' }" :aria-pressed="String(state.statsRange === 'today')" @click="setRange('today')">今日数据 (Today)</button>
      <button class="range-button" :class="{ selected: state.statsRange === 'all' }" :aria-pressed="String(state.statsRange === 'all')" @click="setRange('all')">全部历史 (All Time)</button>
    </div>

    <div class="stats-cards">
      <article>
        <span>上下文缓存命中率</span>
        <strong>{{ rate(data.cache_hit_rate) }}</strong>
        <small>提示词前缀命中复用比</small>
      </article>
      <article>
        <span>{{ state.statsRange === "today" ? "今日" : "累计" }}请求数 &amp; 成功率</span>
        <strong>{{ number(data.requests || 0) }} 次</strong>
        <small>成功率 {{ rate(data.success_rate) }} · 失败 {{ number(data.failed || 0) }}</small>
      </article>
      <article>
        <span>生成速度</span>
        <strong>{{ data.tokens_per_second == null ? "—" : data.tokens_per_second.toFixed(1) }}</strong>
        <small>{{ data.decode_tokens ? `已统计 ${number(data.decode_tokens)} 输出 tokens` : "暂无带用量与计时的流式请求" }}</small>
      </article>
      <article>
        <span>端到端耗时</span>
        <strong>{{ seconds(data.avg_duration_ms) }}</strong>
        <small>平均单次 · 共 {{ number(data.requests || 0) }} 次请求</small>
      </article>
    </div>

    <div class="stats-detail">
      <span>首字延迟 <b>{{ data.ttft_p50_ms == null ? "—" : `${seconds(data.ttft_p50_ms)} (P50 ${seconds(data.ttft_avg_ms)})` }}</b></span>
      <span>提示 tokens <b>{{ number(data.prompt_tokens || 0) }}</b></span>
      <span>输出 tokens <b>{{ number(data.completion_tokens || 0) }}</b></span>
      <span>其中思考 <b>{{ number(data.reasoning_tokens || 0) }}</b></span>
      <span>缓存命中 tokens <b>{{ number(data.cache_hit_tokens || 0) }}</b></span>
      <span class="stats-window">
        {{ state.statsRange === "today" ? `统计日期 ${data.date || "—"}（北京时间）` : `覆盖 ${data.days || 0} 天${data.since ? " · 自 " + data.since : ""}` }}
      </span>
    </div>

    <div class="section-toolbar">
      <div>
        <h2>按模型统计</h2>
        <p>流式请求的首字延迟、生成速度、耗时与 Token 汇总。</p>
      </div>
    </div>

    <div class="table-wrap">
      <table class="stats-table">
        <thead>
          <tr>
            <th>模型</th>
            <th>输出（思考）</th>
            <th>首字延迟 (TTFT)</th>
            <th>生成速度</th>
            <th>端到端耗时</th>
            <th>缓存命中</th>
            <th class="align-right">请求</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.model">
            <td>
              <strong>{{ row.model }}</strong>
              <small class="cell-note">成功率 {{ rate(row.success_rate) }}</small>
            </td>
            <td class="mono">
              {{ number(row.completion_tokens) }}
              <small v-if="row.reasoning_tokens" class="cell-note">思考 {{ number(row.reasoning_tokens) }}</small>
            </td>
            <td class="mono">
              <template v-if="row.ttft_p50_ms == null">—</template>
              <template v-else>
                {{ seconds(row.ttft_p50_ms) }}
                <small class="cell-note">P50 {{ seconds(row.ttft_avg_ms) }}</small>
              </template>
            </td>
            <td class="mono speed">
              <template v-if="row.tokens_per_second == null">—</template>
              <template v-else>
                {{ row.tokens_per_second.toFixed(1) }}
                <small class="cell-note">tok/s</small>
              </template>
            </td>
            <td class="mono">{{ seconds(row.avg_duration_ms) }}</td>
            <td class="mono">
              {{ rate(row.cache_hit_rate) }}
              <small class="cell-note">{{ number(row.cache_hit_tokens) }} tokens</small>
            </td>
            <td class="align-right mono">{{ number(row.requests) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!rows.length" class="empty">
        <span class="empty-icon">◷</span>
        <h3>还没有可统计的请求</h3>
        <p>发起一次 API 调用或连接测试后，这里会按模型汇总首字延迟与生成速度。</p>
      </div>
    </div>

    <div class="section-toolbar">
      <div>
        <h2>按账号统计</h2>
        <p>账号池中各账号处理的请求量、Token 用量与生成速度。</p>
      </div>
    </div>

    <div class="table-wrap">
      <table class="stats-table">
        <thead>
          <tr>
            <th>账号</th>
            <th>输出（思考）</th>
            <th>首字延迟 (TTFT)</th>
            <th>生成速度</th>
            <th>端到端耗时</th>
            <th>缓存命中</th>
            <th class="align-right">请求</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in accountRows" :key="row.model">
            <td>
              <strong>{{ row.model }}</strong>
              <small class="cell-note">成功率 {{ rate(row.success_rate) }}</small>
            </td>
            <td class="mono">
              {{ number(row.completion_tokens) }}
              <small v-if="row.reasoning_tokens" class="cell-note">思考 {{ number(row.reasoning_tokens) }}</small>
            </td>
            <td class="mono">
              <template v-if="row.ttft_p50_ms == null">—</template>
              <template v-else>
                {{ seconds(row.ttft_p50_ms) }}
                <small class="cell-note">P50 {{ seconds(row.ttft_avg_ms) }}</small>
              </template>
            </td>
            <td class="mono speed">
              <template v-if="row.tokens_per_second == null">—</template>
              <template v-else>
                {{ row.tokens_per_second.toFixed(1) }}
                <small class="cell-note">tok/s</small>
              </template>
            </td>
            <td class="mono">{{ seconds(row.avg_duration_ms) }}</td>
            <td class="mono">
              {{ rate(row.cache_hit_rate) }}
              <small class="cell-note">{{ number(row.cache_hit_tokens) }} tokens</small>
            </td>
            <td class="align-right mono">{{ number(row.requests) }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!accountRows.length" class="empty">
        <span class="empty-icon">◷</span>
        <h3>还没有账号维度的统计</h3>
        <p>账号池处理请求后，这里会按账号汇总用量与生成速度。</p>
      </div>
    </div>

    <div class="note-row">
      <span>ⓘ</span>
      <p>仅汇总请求数量、耗时与上游返回的 Token 计数；不记录提示词、回复内容或密钥。账号维度按处理请求的账号展示名汇总。今日数据按北京时间划分，统计按日保留最近 90 天。</p>
    </div>
  </section>
</template>
