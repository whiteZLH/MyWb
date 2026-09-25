<script setup>
import { reactive, ref, watch } from "vue";
import { api } from "../api.js";
import { copy } from "../clipboard.js";
import { refresh, showToast } from "../store.js";
import { ui } from "../ui.js";

const SITES = [
  { value: "cn", label: "国内 CodeBuddy（copilot.tencent.com）" },
  { value: "cn-work", label: "国内 WorkBuddy（workbuddy.cn）" },
  { value: "intl", label: "国际 WorkBuddy AI（workbuddy.ai）" },
  { value: "intl-codebuddy", label: "国际 CodeBuddy（codebuddy.ai）" },
];

const DEFAULT_STATUS = "登录链接 5 分钟内有效。同一账号重新登录会更新凭据。";

const dialog = ref(null);
const mode = ref("browser");
const name = ref("");
const site = ref(SITES[0].value);
const error = ref("");
const dragOver = ref(false);
const fileName = ref("");
const json = ref("");
const oauth = reactive({ flow: null, status: DEFAULT_STATUS, warning: false, busy: false, retry: false });
const importBusy = ref(false);

let timer = null;
let generation = 0;

function clearOAuth(cancelRemote = true) {
  generation += 1;
  clearTimeout(timer);
  const previous = oauth.flow;
  oauth.flow = null;
  if (cancelRemote && previous) api("oauth/" + previous.id, { method: "DELETE" }).catch(() => {});
  oauth.status = DEFAULT_STATUS;
  oauth.warning = false;
  oauth.retry = false;
  oauth.busy = false;
}

function resetAll() {
  clearOAuth();
  mode.value = "browser";
  name.value = "";
  site.value = SITES[0].value;
  error.value = "";
  fileName.value = "";
  json.value = "";
  dragOver.value = false;
}

watch(
  () => ui.accountOpen,
  (open) => {
    if (open) {
      resetAll();
      dialog.value?.showModal();
    } else {
      dialog.value?.open && dialog.value.close();
    }
  }
);

function close() {
  ui.accountOpen = false;
}

async function poll() {
  if (!oauth.flow) return;
  const mine = generation;
  const flow = oauth.flow;
  if (Date.now() >= flow.expires_at) {
    clearOAuth(false);
    oauth.status = "登录链接已过期，请重新生成。";
    return;
  }
  try {
    const result = await api("oauth/" + flow.id + "/poll", { method: "POST" });
    if (mine !== generation) return;
    if (result.status === "success") {
      close();
      await refresh();
      showToast(result.updated ? "授权成功，账号凭据已更新" : "授权成功，账号已添加；可在列表中切换使用");
      return;
    }
    if (result.status === "expired") {
      clearOAuth(false);
      oauth.status = "登录链接已过期，请重新生成。";
      return;
    }
    oauth.warning = false;
    oauth.status = `等待你在官方页面完成登录… 链接剩余 ${Math.max(1, Math.ceil((flow.expires_at - Date.now()) / 1000))} 秒。`;
    timer = setTimeout(poll, 3000);
  } catch (e) {
    if (mine !== generation) return;
    oauth.warning = true;
    oauth.status = e.message;
    oauth.retry = true;
  }
}

async function startOAuth() {
  clearOAuth();
  const mine = generation;
  oauth.busy = true;
  error.value = "";
  try {
    const flow = await api("oauth/start", {
      method: "POST",
      body: { name: name.value.trim() || undefined, site: site.value },
    });
    if (mine !== generation) {
      api("oauth/" + flow.id, { method: "DELETE" }).catch(() => {});
      return;
    }
    oauth.flow = flow;
    oauth.status = "登录链接已就绪，请打开官方页面完成登录。";
    timer = setTimeout(poll, 3000);
  } catch (e) {
    if (mine === generation) error.value = e.message;
  } finally {
    if (mine === generation) oauth.busy = false;
  }
}

async function readFile(file) {
  if (!file) return;
  if (file.size > 1024 * 1024) throw new Error("文件超过 1 MB，请选择登录凭据文件");
  json.value = (await file.text()).replace(/^\uFEFF/, "");
  fileName.value = file.name;
}

function onPick(event) {
  readFile(event.target.files[0]).catch((e) => (error.value = e.message));
}

function onDrop(event) {
  dragOver.value = false;
  readFile(event.dataTransfer.files[0]).catch((e) => (error.value = e.message));
}

async function importAccount() {
  error.value = "";
  importBusy.value = true;
  try {
    let credential;
    try {
      credential = JSON.parse(json.value);
    } catch {
      throw new Error("请选择文件或粘贴有效的 JSON 内容");
    }
    await api("accounts", {
      method: "POST",
      body: { name: name.value.trim() || undefined, credential },
    });
    close();
    await refresh();
    showToast("账号已导入");
  } catch (e) {
    error.value = e.message;
  } finally {
    importBusy.value = false;
  }
}

async function copyLink() {
  if (oauth.flow) showToast((await copy(oauth.flow.url)) ? "已复制" : "无法自动复制，请手动选择文本复制");
}
</script>

<template>
  <dialog ref="dialog" class="account-dialog" @close="clearOAuth()">
    <form @submit.prevent="mode === 'file' ? importAccount() : null">
      <div class="dialog-heading">
        <div>
          <span class="eyebrow">ADD ACCOUNT</span>
          <h2>添加账号</h2>
        </div>
        <button type="button" class="icon-button" aria-label="关闭" @click="close">×</button>
      </div>
      <p class="muted">在官方页面完成登录，授权凭据保存在你的服务器。</p>

      <div class="account-modes" role="group" aria-label="选择添加账号方式">
        <button type="button" :aria-pressed="String(mode === 'browser')" @click="mode = 'browser'; clearOAuth()">
          ↗ 浏览器登录
        </button>
        <button type="button" :aria-pressed="String(mode === 'file')" @click="mode = 'file'; clearOAuth()">
          ⇧ 导入文件
        </button>
      </div>

      <label for="account-name">账号备注 <span class="muted">（选填）</span></label>
      <input id="account-name" v-model="name" maxlength="60" placeholder="例如：日常开发、备用账号" />

      <label for="oauth-site">登录站点</label>
      <select id="oauth-site" v-model="site" :disabled="!!oauth.flow">
        <option v-for="s in SITES" :key="s.value" :value="s.value">{{ s.label }}</option>
      </select>
      <p class="muted">请选择与该账号订阅一致的站点；账号实际归属会以授权结果校验。</p>

      <section v-if="mode === 'browser'">
        <div class="oauth-step">
          <span class="step-number">1</span>
          <div>
            <h3>在 WorkBuddy 完成登录</h3>
            <p class="muted">
              生成专属链接，在官方页面完成扫码或账号登录。支持国内 / 国际 CodeBuddy 与 WorkBuddy 账号，请先选择登录站点。
            </p>
            <button type="button" class="secondary" :disabled="oauth.busy" @click="startOAuth">
              {{ oauth.flow ? "重新生成链接" : "生成登录链接 ↗" }}
            </button>
            <div v-if="oauth.flow" class="oauth-link-box">
              <a class="primary oauth-open" :href="oauth.flow.url" target="_blank" rel="noopener noreferrer">打开官方登录页面 ↗</a>
              <button type="button" class="text-button" @click="copyLink">复制链接</button>
            </div>
          </div>
        </div>
        <div class="oauth-step">
          <span class="step-number">2</span>
          <div>
            <h3>返回这里，自动保存账号</h3>
            <p class="muted">完成登录后保持此窗口打开，系统会自动获取授权结果，无需复制回调地址。</p>
          </div>
        </div>
        <div class="banner oauth-status" :class="{ warning: oauth.warning }" role="status" aria-live="polite">
          {{ oauth.status }}
        </div>
        <button v-if="oauth.retry" type="button" class="secondary oauth-retry" @click="oauth.retry = false; poll()">
          重新检查授权状态
        </button>
      </section>

      <div v-else>
        <label
          class="file-drop"
          :class="{ 'drag-over': dragOver }"
          @dragover.prevent="dragOver = true"
          @dragleave="dragOver = false"
          @drop.prevent="onDrop"
        >
          <span>↥</span>
          <strong>{{ fileName || "选择或拖入 .info / .json 文件" }}</strong>
          <small>文件最大 1 MB · 内容仅传入你的服务器</small>
          <input type="file" accept=".info,.json,application/json" @change="onPick" />
        </label>
        <details>
          <summary>或粘贴 JSON 内容</summary>
          <label class="sr-only" for="credential-json">登录凭据 JSON</label>
          <textarea
            id="credential-json"
            v-model="json"
            rows="6"
            placeholder='包含 "auth" 和 "account" 的完整 JSON'
            spellcheck="false"
          ></textarea>
        </details>
      </div>

      <p class="field-error" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="secondary" @click="close">取消</button>
        <button v-if="mode === 'file'" type="submit" class="primary" :disabled="importBusy">导入账号</button>
      </div>
    </form>
  </dialog>
</template>
