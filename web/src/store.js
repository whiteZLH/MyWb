/**
 * 全局响应式状态。集中承载会话、概览数据、账号池设置与测试记录，
 * 供给各页面组件共享（避免旧实现里散落的全局变量与手工 DOM 同步）。
 */
import { computed, reactive, readonly } from "vue";
import { api, setCsrf } from "./api.js";

export const state = reactive({
  /** 登录态：null=正在探测，false=未登录，true=已登录 */
  authed: null,
  booting: true,
  overview: null,
  stats: null,
  requests: null,
  statsRange: "today",
  loading: false,
  loadError: "",
  toast: "",
  page: "dashboard",
});

export const accounts = computed(() => state.overview?.accounts ?? []);
export const keys = computed(() => state.overview?.keys ?? []);
export const models = computed(() => state.overview?.models ?? []);
export const events = computed(() => state.overview?.events ?? []);
export const metrics = computed(() => state.overview?.metrics ?? {});
export const pool = computed(() => state.overview?.pool ?? {});
export const nodes = computed(() => state.overview?.nodes ?? []);
export const vless = computed(() => state.overview?.vless ?? { nodes: [], running: false, error: null });

/** 当前被指定为“手动 / 测试”的启用账号。 */
export const activeAccount = computed(
  () => accounts.value.find((a) => a.active && a.enabled) ?? null
);

let toastTimer = null;
export function showToast(message) {
  state.toast = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    state.toast = "";
  }, 4500);
}

export const PAGES = {
  dashboard: ["让每个账号，各尽其用", "MYWB WORKSPACE", "在这里查看服务运行、账号积分和请求表现。"],
  stats: ["运行统计", "RUNTIME STATS", "查看首字延迟、生成速度、缓存命中与 Token 用量。"],
  requests: ["请求记录", "REQUEST LOG", "逐条查看最近的调用：来源、首字延迟、耗时与 Token 消耗。"],
  accounts: ["账号池", "ACCOUNT POOL", "集中管理登录凭据、积分与可用状态，让请求自动分配到可用账号。"],
  nodes: ["节点池", "PROXY NODES", "批量维护 VLESS 节点，探测可用性，自动或手动分配到账号。"],
  keys: ["API 密钥", "CLIENT ACCESS", "为每个客户端分配独立密钥，让连接清晰可控。"],
  test: ["连接测试", "CONNECTION LAB", "从当前账号发起请求，确认模型能否正常响应。"],
  guide: ["接入指南", "GET CONNECTED", "从导入凭据到客户端接入，只需几步。"],
};

export async function refresh() {
  state.loading = true;
  try {
    state.overview = await api("overview");
    state.loadError = "";
  } catch (error) {
    state.loadError = error.message;
    throw error;
  } finally {
    state.loading = false;
  }
}

export async function loadStats() {
  state.stats = await api("stats");
}

export async function loadRequests() {
  state.requests = (await api("requests")).requests ?? [];
}

/** 登录成功后写入会话并拉取首屏数据。 */
export async function enter(token) {
  setCsrf(token);
  state.authed = true;
  state.booting = false;
  await refresh();
}

/** 退出登录 / 会话失效：回到登录页并清空敏感数据。 */
export function leave() {
  setCsrf("");
  state.authed = false;
  state.overview = null;
  state.stats = null;
  state.requests = null;
  state.booting = false;
  state.toast = "";
}

export function goPage(next) {
  if (!(next in PAGES)) next = "dashboard";
  state.page = next;
  history.replaceState(null, "", "#" + next);
  if (next === "stats") loadStats().catch((e) => showToast(e.message));
  if (next === "requests") loadRequests().catch((e) => showToast(e.message));
}
