<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { api, setUnauthorizedHandler } from "./api.js";
import {
  enter,
  goPage,
  leave,
  PAGES,
  refresh,
  showToast,
  state,
} from "./store.js";
import LoginScreen from "./components/LoginScreen.vue";
import DashboardView from "./views/DashboardView.vue";
import StatsView from "./views/StatsView.vue";
import RequestsView from "./views/RequestsView.vue";
import AccountsView from "./views/AccountsView.vue";
import KeysView from "./views/KeysView.vue";
import TestView from "./views/TestView.vue";
import GuideView from "./views/GuideView.vue";
import AccountDialog from "./components/AccountDialog.vue";
import KeyDialog from "./components/KeyDialog.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";

const NAV = [
  { key: "dashboard", label: "概览", icon: "▦" },
  { key: "stats", label: "运行统计", icon: "◷" },
  { key: "requests", label: "请求记录", icon: "≡" },
  { key: "accounts", label: "账号池", icon: "◫" },
  { key: "keys", label: "API 密钥", icon: "⌘" },
  { key: "test", label: "连接测试", icon: "↗" },
  { key: "guide", label: "接入指南", icon: "☰" },
];

const VIEWS = {
  dashboard: DashboardView,
  stats: StatsView,
  requests: RequestsView,
  accounts: AccountsView,
  keys: KeysView,
  test: TestView,
  guide: GuideView,
};

const host = ref(location.host);
const currentView = computed(() => VIEWS[state.page] ?? DashboardView);
const meta = computed(() => PAGES[state.page] ?? PAGES.dashboard);
const crumb = computed(() => (state.page === "dashboard" ? "概览" : meta.value[0]));

let pollTimer = null;

async function handleLogin(key) {
  const data = await api("login", { method: "POST", body: { key: key.trim() } });
  await enter(data.csrf);
}

async function logout() {
  try {
    await api("logout", { method: "POST" });
  } catch (error) {
    showToast(error.message);
  } finally {
    leave();
  }
}

async function onRefresh() {
  try {
    await refresh();
  } catch (error) {
    showToast(error.message);
  }
}

onMounted(async () => {
  setUnauthorizedHandler(leave);
  try {
    const session = await api("session");
    await enter(session.csrf);
    goPage(location.hash.slice(1));
  } catch {
    leave();
  }
  pollTimer = setInterval(() => {
    const dialogOpen = document.querySelector("dialog[open]");
    if (state.authed && state.overview && !document.hidden && !dialogOpen) {
      refresh().catch(() => {});
    }
  }, 30000);
});

onBeforeUnmount(() => clearInterval(pollTimer));
</script>

<template>
  <div v-if="state.booting" class="boot">
    <span class="brand-symbol">M<span>↗</span></span>
    <p>正在连接工作空间…</p>
  </div>

  <LoginScreen v-else-if="!state.authed" :submit="handleLogin" />

  <div v-else class="app-shell">
    <aside class="sidebar">
      <a class="brand" href="/admin/">
        <span class="brand-symbol">M<span>↗</span></span>
        <span>MYWB<small>管理控制台</small></span>
      </a>
      <div class="nav-caption">工作空间</div>
      <nav aria-label="主要导航">
        <button
          v-for="item in NAV"
          :key="item.key"
          :data-page="item.key"
          class="nav-item"
          :class="{ selected: state.page === item.key }"
          :aria-current="state.page === item.key ? 'page' : 'false'"
          @click="goPage(item.key)"
        >
          <span>{{ item.icon }}</span>{{ item.label }}
        </button>
      </nav>
      <div class="sidebar-bottom">
        <span class="tag">PRIVATE WORKSPACE</span>
        <p><i class="status-dot"></i> 服务已连接</p>
        <button class="logout" @click="logout">退出登录 <span>↗</span></button>
      </div>
    </aside>

    <div class="main-shell">
      <header class="topbar">
        <div>
          <span class="muted">工作空间</span>
          <span class="slash">/</span>
          <span>{{ crumb }}</span>
        </div>
        <div class="topbar-right">
          <span class="mono">{{ host }}</span>
          <span class="avatar">W</span>
        </div>
      </header>

      <main>
        <div class="page-title">
          <div>
            <span class="eyebrow">{{ meta[1] }}</span>
            <h1>{{ meta[0] }}<span class="title-dot">.</span></h1>
            <p class="muted">{{ meta[2] }}</p>
          </div>
          <button class="secondary" :disabled="state.loading" @click="onRefresh">
            <span>↻</span> 刷新状态
          </button>
        </div>

        <div v-if="state.loadError" class="banner warning" role="alert">{{ state.loadError }}</div>

        <component :is="currentView" />
        <footer class="main-footer">
          <span>MYWB CONSOLE</span>
          <span>凭据管理 · 安全连接 · 即时验证</span>
        </footer>
      </main>
    </div>
  </div>

  <template v-if="state.authed">
    <AccountDialog />
    <KeyDialog />
    <ConfirmDialog />
  </template>

  <div v-if="state.toast" class="toast" role="status">{{ state.toast }}</div>
</template>
