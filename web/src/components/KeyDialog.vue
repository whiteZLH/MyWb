<script setup>
import { ref, watch } from "vue";
import { api } from "../api.js";
import { copy } from "../clipboard.js";
import { refresh, showToast } from "../store.js";
import { ui } from "../ui.js";

const dialog = ref(null);
const name = ref("");
const error = ref("");
const createdKey = ref("");
const busy = ref(false);

watch(
  () => ui.keyOpen,
  (open) => {
    if (open) {
      name.value = "";
      createdKey.value = "";
      error.value = "";
      dialog.value?.showModal();
    } else {
      dialog.value?.open && dialog.value.close();
    }
  }
);

function close() {
  ui.keyOpen = false;
}

async function onSubmit() {
  error.value = "";
  busy.value = true;
  try {
    const data = await api("keys", { method: "POST", body: { name: name.value } });
    createdKey.value = data.key;
    await refresh();
  } catch (e) {
    error.value = e.message;
  } finally {
    busy.value = false;
  }
}

async function onCopy() {
  showToast((await copy(createdKey.value)) ? "已复制" : "无法自动复制，请手动选择文本复制");
}
</script>

<template>
  <dialog ref="dialog" @close="close">
    <form v-if="!createdKey" @submit.prevent="onSubmit">
      <div class="dialog-heading">
        <div>
          <span class="eyebrow">NEW API KEY</span>
          <h2>创建客户端密钥</h2>
        </div>
        <button type="button" class="icon-button" aria-label="关闭" @click="close">×</button>
      </div>
      <label for="key-name">密钥名称</label>
      <input id="key-name" v-model="name" placeholder="例如：Cherry Studio" maxlength="60" required />
      <p class="field-error" role="alert">{{ error }}</p>
      <div class="dialog-actions">
        <button type="button" class="secondary" @click="close">取消</button>
        <button type="submit" class="primary" :disabled="busy">创建密钥</button>
      </div>
    </form>

    <div v-else>
      <span class="eyebrow">KEY CREATED</span>
      <h2>保存你的新密钥</h2>
      <p class="muted">关闭后无法再次查看完整内容。请复制到安全的位置。</p>
      <div class="secret-value"><code>{{ createdKey }}</code></div>
      <div class="dialog-actions">
        <button class="secondary" @click="onCopy">复制密钥</button>
        <button class="primary" @click="close">我已保存</button>
      </div>
    </div>
  </dialog>
</template>
