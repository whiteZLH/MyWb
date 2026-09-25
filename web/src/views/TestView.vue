<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../api.js";
import { activeAccount, events, models, refresh, showToast } from "../store.js";
import { stamp } from "../format.js";

const form = reactive({ model: "deepseek-v4-flash", prompt: "请只回复：连接成功" });
const busy = ref(false);
const status = ref("等待请求");
const statusTone = ref("pill");
const output = ref("选择模型并发送消息，结果将在这里显示。");
const meta = ref("");

watch(
  () => models.value,
  (list) => {
    if (list.length && !list.includes(form.model)) form.model = list.includes("deepseek-v4-flash") ? "deepseek-v4-flash" : list[0];
  },
  { immediate: true }
);

const disabled = computed(() => !activeAccount.value || busy.value);
const activeName = computed(() => activeAccount.value?.name || "尚未选择");

async function onSubmit() {
  busy.value = true;
  status.value = "请求中";
  statusTone.value = "pill amber";
  output.value = "正在等待上游响应，最长约 90 秒…";
  meta.value = "";
  try {
    const r = await api("test", { method: "POST", body: { model: form.model, prompt: form.prompt } });
    statusTone.value = "pill " + (r.ok ? "green" : "red");
    status.value = r.ok ? "连接成功" : "调用失败";
    output.value = r.ok ? r.answer : r.error;
    const tokens = r.usage?.total_tokens !== undefined ? " · " + r.usage.total_tokens + " tokens" : "";
    meta.value = `${r.seconds}s${r.status ? " · HTTP " + r.status : ""}${tokens}`;
  } catch (error) {
    statusTone.value = "pill red";
    status.value = "请求失败";
    output.value = error.message;
  } finally {
    busy.value = false;
    await refresh().catch(() => {});
  }
}
</script>

<template>
  <section class="page-panel">
    <div class="test-layout">
      <form class="surface test-form" @submit.prevent="onSubmit">
        <span class="eyebrow">LIVE REQUEST</span>
        <h2>发送一条真实请求</h2>
        <p class="muted">使用当前选中的账号，测试会消耗少量上游额度。</p>
        <div class="active-strip">当前账号 <strong>{{ activeName }}</strong></div>
        <label for="model">测试模型</label>
        <select id="model" v-model="form.model" required>
          <option v-for="m in models" :key="m" :value="m">{{ m }}</option>
        </select>
        <label for="test-prompt">消息</label>
        <textarea id="test-prompt" v-model="form.prompt" rows="5" maxlength="2000" required></textarea>
        <button class="primary wide" type="submit" :disabled="disabled">
          {{ busy ? "正在调用…" : "发送测试 ↗" }}
        </button>
      </form>

      <article class="surface response-surface">
        <div class="response-header">
          <h2>响应结果</h2>
          <span :class="statusTone">{{ status }}</span>
        </div>
        <pre class="test-output">{{ output }}</pre>
        <div class="muted mono">{{ meta }}</div>
      </article>
    </div>

    <div class="section-toolbar">
      <div>
        <h2>本次运行的测试记录</h2>
        <p>仅记录状态与耗时，不记录测试消息或回复。</p>
      </div>
    </div>

    <div class="history">
      <div v-for="(e, i) in events" :key="i" class="history-row">
        <span class="mono muted">{{ stamp(e.time * 1000) }}</span>
        <strong>{{ e.model }}</strong>
        <span class="pill" :class="e.ok ? 'green' : 'red'">{{ e.ok ? "成功" : "失败" }}</span>
        <span class="mono">{{ e.seconds }}s</span>
      </div>
      <p v-if="!events.length" class="history-empty">暂无测试记录，发送第一条测试消息。</p>
    </div>
  </section>
</template>
